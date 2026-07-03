"""임의 은행 양식 해석기: AI가 컬럼 구조를 제안하고, 사람이 승인하고, 수학이 검증한다.

어떤 은행의 CSV/XLSX가 오더라도:
1) 이미 승인된 양식(원장 DB에 기억) 또는 내장 별칭으로 즉시 해석을 시도하고,
2) 모르는 양식이면 Gemini에게 '컬럼 이름 목록만' 보내 매핑을 제안받고(금액·거래처·수취인은 전송하지 않음),
3) 사람이 확인한 매핑만 사용하며, 크로스체크까지 통과한 매핑만 자동 재사용된다.
API 키가 없으면 AI 단계는 건너뛰고 사람에게 직접 묻는다(오프라인 완주 원칙).

안전장치: 헤더 행이 없어 보이는 파일(첫 행이 날짜/금액 데이터)은 LLM에 보내지 않고
합성 컬럼명(Column_n)으로 수동 매핑만 허용한다 — 실거래 데이터가 밖으로 나가지 않게.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from pathlib import Path

import pandas as pd

from .data_normalizer import DataNormalizerAgent
from .security import MAX_CSV_BYTES

REQUIRED_FIELDS = ["date", "description", "withdrawal", "deposit"]
OPTIONAL_FIELDS = ["balance", "account"]
FIELD_LABELS = {
    "date": "날짜", "description": "적요/내용", "withdrawal": "출금액",
    "deposit": "입금액", "balance": "잔액", "account": "은행/계좌",
}
CANONICAL_HEADERS = {
    "date": "거래일시", "description": "기재내용", "withdrawal": "찾으신금액(출금)",
    "deposit": "맡기신금액(입금)", "balance": "잔액", "account": "계좌",
}
MAX_ROWS = 200_000
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f  ]")
_DATE_LIKE = re.compile(r"^\d{4}[-./]\d{1,2}[-./]\d{1,2}")
_NUMBER_LIKE = re.compile(r"^-?[\d,]+(\.\d+)?$")

_ALIAS_SET = {
    DataNormalizerAgent._clean_name(alias)
    for aliases in DataNormalizerAgent.COLUMN_ALIASES.values()
    for alias in aliases
}


def _sanitize_headers(cells: list) -> list[str]:
    """헤더 이름에서 제어문자를 제거하고, 비어 있거나 중복된 이름을 유일하게 만든다."""
    headers: list[str] = []
    seen: dict[str, int] = {}
    for i, cell in enumerate(cells, 1):
        name = "" if cell is None or (isinstance(cell, float) and pd.isna(cell)) else str(cell)
        name = _CONTROL_CHARS.sub(" ", name).strip()[:60]
        if not name or name.lower() == "nan":
            name = f"Unnamed_{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        headers.append(name)
    return headers


def _alias_score(cells: list) -> int:
    return sum(1 for c in cells if DataNormalizerAgent._clean_name(c) in _ALIAS_SET)


def _looks_like_data(cells: list[str]) -> bool:
    hits = sum(1 for c in cells if _DATE_LIKE.match(c) or _NUMBER_LIKE.match(c))
    return hits >= 2


def _find_header_row(raw: pd.DataFrame) -> int | None:
    """별칭 매칭 점수가 가장 높은 행을 헤더로 고른다. 없으면 휴리스틱, 그래도 없으면 None."""
    best_idx, best_score = None, 0
    for idx in range(min(15, len(raw))):
        cells = _sanitize_headers(list(raw.iloc[idx]))
        score = _alias_score(cells)
        if score > best_score:
            best_idx, best_score = idx, score
    if best_score >= 2:
        return best_idx
    for idx in range(min(10, len(raw))):
        cells = [c for c in _sanitize_headers(list(raw.iloc[idx])) if not c.startswith("Unnamed_")]
        if len(cells) >= 3 and not _looks_like_data(cells):
            return idx
    return None


def _assemble(raw: pd.DataFrame, header_idx: int | None) -> pd.DataFrame:
    if header_idx is None:  # 헤더 없음: 합성 컬럼명 (LLM 전송 금지 대상)
        body = raw.reset_index(drop=True)
        body.columns = [f"Column_{i}" for i in range(1, body.shape[1] + 1)]
        body.attrs["headerless"] = True
    else:
        body = raw.iloc[header_idx + 1:].reset_index(drop=True)
        body.columns = _sanitize_headers(list(raw.iloc[header_idx]))
        body.attrs["headerless"] = False
    if len(body) > MAX_ROWS:
        raise ValueError(f"파일 행 수가 상한({MAX_ROWS:,}행)을 넘습니다.")
    return body.dropna(how="all")


def read_table(path: str | Path) -> pd.DataFrame:
    """CSV/XLSX를 읽어 헤더를 찾아낸 표를 돌려준다. 헤더가 없으면 attrs['headerless']=True."""
    path = Path(path)
    if path.suffix.lower() == ".xlsx":
        with zipfile.ZipFile(path) as archive:  # 압축 해제 크기 기준 방어 (zip 폭탄)
            if sum(info.file_size for info in archive.infolist()) > MAX_CSV_BYTES * 4:
                raise ValueError("엑셀 파일의 압축 해제 크기가 허용 한도를 넘습니다.")
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=str)
        best_name, best_idx, best_score = None, None, -1
        for name, raw in sheets.items():
            if raw.empty:
                continue
            idx = _find_header_row(raw)
            score = _alias_score(_sanitize_headers(list(raw.iloc[idx]))) if idx is not None else -1
            if score > best_score:
                best_name, best_idx, best_score = name, idx, score
        if best_name is None:
            raise ValueError("엑셀 파일에서 표를 찾지 못했습니다.")
        if len(sheets) > 1:
            print(f"  (여러 시트 중 '{best_name}' 시트를 해석 대상으로 선택했습니다)")
        return _assemble(sheets[best_name], best_idx)

    raw = None
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            raw = pd.read_csv(path, encoding=encoding, dtype=str, header=None)
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    if raw is None:
        raise ValueError(f"파일 인코딩을 읽을 수 없습니다: {last_error}")
    return _assemble(raw, _find_header_row(raw))


def format_signature(headers: list[str]) -> str:
    key = "|".join(sorted(DataNormalizerAgent._clean_name(h) for h in headers))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def resolve_mapping(mapping: dict, columns: list[str]) -> dict:
    """저장된/제안된 매핑의 헤더 이름을 실제 컬럼 이름으로 재탐색한다 (공백·대소문자 무시)."""
    lookup = {DataNormalizerAgent._clean_name(c): c for c in columns}
    resolved: dict = {}
    missing: list[str] = []
    for field, header in mapping.items():
        if not header:
            continue
        actual = lookup.get(DataNormalizerAgent._clean_name(header))
        if actual:
            resolved[field] = actual
        elif field in REQUIRED_FIELDS:
            missing.append(f"{FIELD_LABELS[field]}({header})")
    if missing:
        raise ValueError(f"저장된 양식의 컬럼을 이 파일에서 찾지 못했습니다: {', '.join(missing)}")
    return resolved


def alias_mapping(headers: list[str]) -> dict | None:
    """내장 별칭 사전으로 즉시 해석 (이미 아는 국내 은행 양식의 빠른 경로)."""
    agent = DataNormalizerAgent()
    mapping = {key: agent._find_column(headers, key) for key in DataNormalizerAgent.COLUMN_ALIASES}
    if all(mapping.get(field) for field in REQUIRED_FIELDS):
        return {k: v for k, v in mapping.items() if v}
    return None


def llm_suggest_mapping(headers: list[str]) -> dict | None:
    """Gemini에게 컬럼 '이름 목록만' 보내 매핑을 제안받는다. 키가 없거나 실패하면 None."""
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")):
        return None
    try:
        from google import genai

        client = genai.Client()
        prompt = (
            "You map bank-statement column headers to standard fields. "
            f"Headers: {json.dumps(headers, ensure_ascii=False)}\n"
            "Return ONLY a JSON object with keys date, description, withdrawal, deposit, balance, account. "
            "Each value must be exactly one of the given headers, or null if absent. "
            "withdrawal = money going out, deposit = money coming in."
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
        data = json.loads(response.text)
    except Exception as exc:  # 네트워크/키/파싱 실패 등 — 사람에게 넘긴다
        print(f"(AI 양식 해석을 사용할 수 없어 수동 매핑으로 전환합니다: {exc})")
        return None
    mapping = {}
    for field in REQUIRED_FIELDS + OPTIONAL_FIELDS:
        value = data.get(field)
        if value in headers:
            mapping[field] = value
    if all(mapping.get(field) for field in REQUIRED_FIELDS):
        return mapping
    return None


def _sample_hints(raw: pd.DataFrame) -> dict[str, str]:
    hints: dict[str, str] = {}
    for column in raw.columns:
        series = raw[column].dropna().astype(str).str.strip()
        series = series[series != ""]
        hints[str(column)] = series.iloc[0][:14] if not series.empty else ""
    return hints


def confirm_mapping(mapping: dict, source: str) -> bool:
    print(f"\n[양식 해석 — {source}] 컬럼을 이렇게 인식했습니다:")
    for field in REQUIRED_FIELDS + OPTIONAL_FIELDS:
        print(f"  {FIELD_LABELS[field]:<7} <- {mapping.get(field) or '(없음)'}")
    return input("이 해석이 맞습니까? [y/N]: ").strip().lower() in {"y", "yes"}


def interactive_mapping(raw: pd.DataFrame) -> dict | None:
    """사람이 직접 컬럼 번호를 골라 매핑한다 (q 입력 시 중단). 예시 값은 화면에만 표시된다."""
    headers = [str(c) for c in raw.columns]
    hints = _sample_hints(raw)
    print("\n[수동 매핑] 파일의 컬럼 목록:")
    for i, header in enumerate(headers, 1):
        hint = f"  (예: {hints[header]})" if hints.get(header) else ""
        print(f"  {i:>2}. {header}{hint}")
    mapping: dict = {}
    for field in REQUIRED_FIELDS + OPTIONAL_FIELDS:
        required = field in REQUIRED_FIELDS
        label = FIELD_LABELS[field]
        while True:
            answer = input(f"'{label}' 컬럼 번호{'' if required else ' (없으면 Enter)'}: ").strip()
            if answer.lower() == "q":
                return None
            if not answer and not required:
                break
            if answer.isdigit() and 1 <= int(answer) <= len(headers):
                mapping[field] = headers[int(answer) - 1]
                break
            print("  잘못된 입력입니다. 컬럼 번호를 입력하세요 (중단: q)")
    return mapping


def to_canonical(raw: pd.DataFrame, mapping: dict, account_label: str | None) -> pd.DataFrame:
    """매핑에 따라 표준 컬럼명(기존 파이프라인 입력 형식)으로 변환한다."""
    out = pd.DataFrame()
    for field, canonical in CANONICAL_HEADERS.items():
        source = mapping.get(field)
        if source and source in raw.columns:
            out[canonical] = raw[source]
        elif field in ("withdrawal", "deposit"):
            out[canonical] = "0"
    if "계좌" not in out.columns or out["계좌"].fillna("").astype(str).str.strip().eq("").all():
        out["계좌"] = account_label or "미지정 계좌"
    return out
