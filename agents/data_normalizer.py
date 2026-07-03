"""은행 CSV 표준화와 검증. 금액 계산은 전부 결정론적으로 수행한다."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class ValidationIssue:
    level: str
    code: str
    message: str
    row: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message, "row": self.row}


@dataclass
class NormalizationResult:
    data: pd.DataFrame
    issues: list[ValidationIssue] = field(default_factory=list)
    current_cash: float | None = None
    cash_basis: str = "확인 불가"
    balance_reliable: bool = False

    @property
    def has_errors(self) -> bool:
        return any(issue.level == "error" for issue in self.issues)


class DataNormalizerAgent:
    """여러 CSV 헤더를 하나의 스키마로 바꾸고 잔액 연속성을 검사한다."""

    COLUMN_ALIASES = {
        "date": ["거래일시", "거래일", "날짜", "거래일자", "일자", "date"],
        "description": ["기재내용", "적요", "내역", "거래내용", "내용", "description"],
        "withdrawal": ["찾으신금액(출금)", "출금액", "출금", "찾으신금액", "withdrawal"],
        "deposit": ["맡기신금액(입금)", "입금액", "입금", "맡기신금액", "deposit"],
        "balance": ["잔액", "거래후잔액", "balance"],
        "account": ["계좌", "계좌번호", "계좌명", "은행", "거래점", "account"],
    }

    def __init__(self, balance_tolerance: float = 1.0):
        self.balance_tolerance = balance_tolerance
        self.last_result: NormalizationResult | None = None

    @staticmethod
    def _clean_name(value: Any) -> str:
        return str(value).strip().lower().replace(" ", "")

    def _find_column(self, columns: list[str], key: str) -> str | None:
        aliases = {self._clean_name(x) for x in self.COLUMN_ALIASES[key]}
        return next((col for col in columns if self._clean_name(col) in aliases), None)

    @staticmethod
    def _amount(series: pd.Series) -> pd.Series:
        cleaned = series.fillna("0").astype(str).str.strip()
        cleaned = cleaned.str.replace(",", "", regex=False).str.replace("원", "", regex=False)
        cleaned = cleaned.replace({"": "0", "-": "0"})
        return pd.to_numeric(cleaned, errors="coerce")

    @staticmethod
    def _balance_amount(series: pd.Series) -> pd.Series:
        """잔액 전용 파서: 빈 칸을 0원이 아니라 '잔액 미보고(NA)'로 보존한다."""
        cleaned = series.astype(str).str.strip()
        cleaned = cleaned.str.replace(",", "", regex=False).str.replace("원", "", regex=False)
        cleaned = cleaned.replace({"": pd.NA, "-": pd.NA, "nan": pd.NA, "None": pd.NA})
        return pd.to_numeric(cleaned, errors="coerce")

    @staticmethod
    def _read_csv(path: Path) -> pd.DataFrame:
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "utf-8", "cp949"):
            try:
                return pd.read_csv(path, encoding=encoding, dtype=str)
            except UnicodeDecodeError as exc:
                last_error = exc
        raise ValueError(f"CSV 인코딩을 읽을 수 없습니다: {last_error}")

    def normalize_with_validation(self, file_path: str | Path) -> NormalizationResult:
        path = Path(file_path)
        if path.suffix.lower() != ".csv":
            raise ValueError("보안을 위해 입력은 CSV 파일만 허용합니다.")
        if not path.is_file():
            raise FileNotFoundError(path)

        raw = self._read_csv(path)
        columns = list(raw.columns)
        found = {key: self._find_column(columns, key) for key in self.COLUMN_ALIASES}
        missing = [key for key in ("date", "description", "withdrawal", "deposit") if not found[key]]
        if missing:
            raise ValueError(f"필수 컬럼을 찾지 못했습니다: {', '.join(missing)}")

        df = pd.DataFrame({
            "Date": pd.to_datetime(raw[found["date"]], errors="coerce"),
            "Description": raw[found["description"]].fillna("").astype(str).str.strip(),
            "Withdrawal": self._amount(raw[found["withdrawal"]]),
            "Deposit": self._amount(raw[found["deposit"]]),
            "SourceRow": range(2, len(raw) + 2),
        })
        df["Balance"] = self._balance_amount(raw[found["balance"]]) if found["balance"] else pd.NA
        df["Account"] = (
            raw[found["account"]].fillna("미지정 계좌").astype(str).str.strip()
            if found["account"] else "미지정 계좌"
        )

        issues: list[ValidationIssue] = []
        for idx, row in df.iterrows():
            source_row = int(row["SourceRow"])
            if pd.isna(row["Date"]):
                issues.append(ValidationIssue("error", "INVALID_DATE", "날짜를 해석할 수 없습니다.", source_row))
            if pd.isna(row["Withdrawal"]) or pd.isna(row["Deposit"]):
                issues.append(ValidationIssue("error", "INVALID_AMOUNT", "금액을 숫자로 해석할 수 없습니다.", source_row))
            elif row["Withdrawal"] < 0 or row["Deposit"] < 0:
                issues.append(ValidationIssue("error", "NEGATIVE_AMOUNT", "입출금액은 음수일 수 없습니다.", source_row))
            elif row["Withdrawal"] > 0 and row["Deposit"] > 0:
                issues.append(ValidationIssue("error", "BOTH_DIRECTIONS", "한 거래에 입금과 출금이 동시에 있습니다.", source_row))
            elif row["Withdrawal"] == 0 and row["Deposit"] == 0:
                issues.append(ValidationIssue("warning", "ZERO_TRANSACTION", "입출금액이 모두 0입니다.", source_row))
            if not row["Description"]:
                issues.append(ValidationIssue("warning", "EMPTY_DESCRIPTION", "거래 설명이 비어 있습니다.", source_row))

        valid = df.dropna(subset=["Date", "Withdrawal", "Deposit"]).copy()
        valid = valid.sort_values(["Date", "SourceRow"], kind="stable").reset_index(drop=True)
        if valid.empty:
            detail = (
                "; ".join(f"{issue.code}(행 {issue.row})" for issue in issues[:5])
                if issues
                else "데이터 행이 없습니다"
            )
            raise ValueError(
                f"CSV에 분석 가능한 거래가 없습니다 (원본 데이터 행 {len(raw)}개). "
                "날짜와 금액 컬럼의 형식을 확인해 주세요. "
                f"발견된 문제: {detail}"
            )
        duplicate_mask = valid.duplicated(["Date", "Description", "Withdrawal", "Deposit", "Account"], keep=False)
        for _, row in valid[duplicate_mask].iterrows():
            issues.append(ValidationIssue("warning", "POSSIBLE_DUPLICATE", "중복 가능성이 있는 거래입니다.", int(row["SourceRow"])))

        continuity_checks = 0
        continuity_failures = 0
        if found["balance"]:
            # 잔액이 없는 중간 행의 입출금도 누적해, 잔액 행 사이의 흐름 전체를 대조한다.
            for _, group in valid.groupby("Account", sort=False):
                group = group.sort_values(["Date", "SourceRow"], kind="stable")
                running: float | None = None
                for _, current in group.iterrows():
                    if running is not None:
                        running += float(current["Deposit"]) - float(current["Withdrawal"])
                    if pd.isna(current["Balance"]):
                        continue
                    if running is None:
                        running = float(current["Balance"])  # 앵커: 첫 보고 잔액
                        continue
                    continuity_checks += 1
                    if abs(running - float(current["Balance"])) > self.balance_tolerance:
                        continuity_failures += 1
                        issues.append(ValidationIssue(
                            "warning", "BALANCE_MISMATCH",
                            f"표시 잔액과 거래 흐름이 {abs(running - float(current['Balance'])):,.0f}원 다릅니다.",
                            int(current["SourceRow"]),
                        ))
                        running = float(current["Balance"])  # 은행 보고값으로 재동기화

        balance_reliable = bool(found["balance"] and continuity_checks > 0 and continuity_failures == 0)
        current_cash: float | None = None
        cash_basis = "확인 불가"
        balance_rows = valid.dropna(subset=["Balance"])
        if not balance_rows.empty and balance_reliable:
            current_cash = float(balance_rows.sort_values(["Date", "SourceRow"]).groupby("Account").tail(1)["Balance"].sum())
            cash_basis = "검증된 계좌별 최신 잔액 합계"
        elif not balance_rows.empty:
            # 신뢰도가 낮아도 계좌를 통째로 누락시키지 않도록 계좌별 마지막 잔액을 합산한다.
            current_cash = float(balance_rows.sort_values(["Date", "SourceRow"]).groupby("Account").tail(1)["Balance"].sum())
            cash_basis = "잔액 불일치 경고 상태의 계좌별 최신 잔액 합계(신뢰도 낮음)"
            issues.append(ValidationIssue("warning", "UNRELIABLE_BALANCE", "계좌별 잔액 연속성이 맞지 않아 최신 표시 잔액 합계를 임시 기준으로 사용합니다."))
        else:
            current_cash = float((valid["Deposit"] - valid["Withdrawal"]).sum())
            cash_basis = "잔액 컬럼 없음: 기간 내 순입출금 누계(기초잔액 포함 필요)"
            issues.append(ValidationIssue("warning", "NO_BALANCE", "잔액 컬럼이 없어 현재 현금의 정확성이 제한됩니다."))

        result = NormalizationResult(valid, issues, current_cash, cash_basis, balance_reliable)
        self.last_result = result
        return result

    def normalize(self, file_path: str | Path) -> pd.DataFrame:
        """이전 코드와 호환되는 간편 인터페이스."""
        return self.normalize_with_validation(file_path).data
