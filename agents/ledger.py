"""SQLite 누적 원장. 엑셀 자금관리 문서의 'Daily' 시트를 데이터베이스로 구현한다.

원칙: 은행이 진실이다. 파일을 원장에 반영한 뒤 계좌별로
[은행이 보고한 잔액] = [원장의 자금 흐름으로 재계산한 잔액]
을 대조하고, 불일치하면 반영 자체를 되돌린다(트랜잭션 롤백).

검증은 '일 단위·순서 무관'으로 수행한다. 은행 파일마다 시각 유무·정렬 방향이
제각각이므로 행 순서에 의존하지 않고, 하루의 순입출금 합계와 그날 보고된
잔액들 사이의 사슬(chain)이 성립하는지를 검사한다. 같은 날 안의 거래 순서가
어떻든 수학적으로 동일한 결과를 준다.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "ledger.db"
BALANCE_TOLERANCE = 1.0


class LedgerMismatch(ValueError):
    """크로스체크 실패: 은행 보고 잔액과 원장 재계산 잔액이 다르다."""

    def __init__(self, message: str, accounts: list[dict]):
        super().__init__(message)
        self.accounts = accounts


class Ledger:
    def __init__(self, path: str | Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS transactions ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " account TEXT NOT NULL,"
            " date TEXT NOT NULL,"
            " description TEXT NOT NULL,"
            " withdrawal REAL NOT NULL,"
            " deposit REAL NOT NULL,"
            " balance REAL,"
            " source_file TEXT NOT NULL,"
            " row_hash TEXT NOT NULL UNIQUE,"
            " imported_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS imports ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " file TEXT NOT NULL,"
            " rows_added INTEGER NOT NULL,"
            " rows_skipped INTEGER NOT NULL,"
            " status TEXT NOT NULL,"
            " detail TEXT,"
            " imported_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS formats ("
            " signature TEXT PRIMARY KEY,"
            " mapping TEXT NOT NULL,"
            " last_account TEXT,"
            " verified INTEGER NOT NULL DEFAULT 0,"
            " approved_at TEXT NOT NULL)"
        )
        try:  # 구버전 DB 마이그레이션
            self.conn.execute("ALTER TABLE formats ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------- 양식 기억 ----------

    def format_record(self, signature: str) -> dict | None:
        row = self.conn.execute(
            "SELECT mapping, last_account, verified FROM formats WHERE signature = ?", (signature,)
        ).fetchone()
        if not row:
            return None
        return {"mapping": row[0], "last_account": row[1], "verified": bool(row[2])}

    def save_format(self, signature: str, mapping_json: str, last_account: str | None, verified: bool) -> None:
        self.conn.execute(
            "INSERT INTO formats (signature, mapping, last_account, verified, approved_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(signature) DO UPDATE SET mapping = excluded.mapping,"
            " last_account = excluded.last_account,"
            " verified = MAX(formats.verified, excluded.verified),"
            " approved_at = excluded.approved_at",
            (signature, mapping_json, last_account, int(verified), datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    # ---------- 반영과 크로스체크 ----------

    def ingest(self, frame: pd.DataFrame, source_file: str) -> dict:
        """정규화된 거래 프레임을 원장에 반영한다. 크로스체크 실패 시 전체 롤백."""
        added = 0
        now = datetime.now().isoformat(timespec="seconds")
        occurrence: dict[str, int] = {}
        try:
            with self.conn:  # 트랜잭션: 예외 발생 시 자동 롤백
                for row in frame.sort_values(["Date", "SourceRow"], kind="stable").itertuples():
                    date_iso = pd.Timestamp(row.Date).isoformat(sep=" ")
                    balance = None if pd.isna(row.Balance) else float(row.Balance)
                    balance_key = "" if balance is None else f"{balance:.2f}"
                    key = (
                        f"{row.Account}|{date_iso}|{row.Description}"
                        f"|{float(row.Withdrawal):.2f}|{float(row.Deposit):.2f}|{balance_key}"
                    )
                    # 같은 파일 안의 '정당한 동일 거래'(같은 날 같은 금액·적요)를 순번으로 구분한다.
                    occurrence[key] = occurrence.get(key, 0) + 1
                    row_hash = hashlib.sha1(f"{key}|{occurrence[key]}".encode("utf-8")).hexdigest()
                    cursor = self.conn.execute(
                        "INSERT OR IGNORE INTO transactions "
                        "(account, date, description, withdrawal, deposit, balance, source_file, row_hash, imported_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (str(row.Account), date_iso, str(row.Description), float(row.Withdrawal),
                         float(row.Deposit), balance, source_file, row_hash, now),
                    )
                    added += cursor.rowcount
                skipped = len(frame) - added

                account_results = [self.verify_account(a) for a in frame["Account"].astype(str).unique()]
                failures = [r for r in account_results if r["status"] == "MISMATCH"]
                if failures:
                    lines = "; ".join(
                        f"{f['account']}: {f['day']} 기준 원장 계산 {f['expected']:,.0f}원 vs 은행 보고 {f['got']:,.0f}원"
                        f" (차이 {abs(f['expected'] - f['got']):,.0f}원)"
                        for f in failures
                    )
                    raise LedgerMismatch(
                        f"크로스체크 불일치로 반영을 중단했습니다 — {lines}. "
                        "해당 계좌의 거래가 빠졌거나(기간 공백), 같은 거래가 이중 반영됐거나, "
                        "출금/입금 컬럼 해석이 뒤집혔을 수 있습니다. 은행 자료가 기준입니다.",
                        account_results,
                    )
        except LedgerMismatch:
            self._log_import(source_file, 0, 0, "REJECTED", "cross-check mismatch")
            raise
        self._log_import(source_file, added, skipped, "OK", None)
        return {"added": added, "skipped": skipped, "accounts": account_results}

    def verify_account(self, account: str) -> dict:
        """계좌의 원장 전체를 일 단위로 재계산해 은행 보고 잔액 사슬과 대조한다.

        반환: status = OK(사슬 검증됨) / OK_ANCHOR_ONLY(잔액 하루뿐이라 방향 검증 불가)
        / NO_BALANCE(잔액 정보 없음) / MISMATCH(불일치). closing은 검증된 마감잔액.
        """
        rows = self.conn.execute(
            "SELECT date, withdrawal, deposit, balance FROM transactions WHERE account = ? ORDER BY date, id",
            (account,),
        ).fetchall()
        days: list[list] = []  # [day, net, balances, nrows]
        for date, withdrawal, deposit, balance in rows:
            day = date[:10]
            if not days or days[-1][0] != day:
                days.append([day, 0.0, [], 0])
            days[-1][1] += float(deposit) - float(withdrawal)
            days[-1][3] += 1
            if balance is not None:
                days[-1][2].append(float(balance))

        base = {"account": account, "closing": None, "closing_date": None, "verified_days": 0,
                "unverified_rows": 0, "day_closings": {}, "expected": None, "got": None, "day": None}
        balanced = [i for i, d in enumerate(days) if d[2]]
        if not balanced:
            return {**base, "status": "NO_BALANCE", "unverified_rows": len(rows)}

        first_b, last_b = balanced[0], balanced[-1]
        unverified = sum(d[3] for d in days[:first_b]) + sum(d[3] for d in days[last_b + 1:])
        first_fail: dict | None = None
        for candidate in dict.fromkeys(days[first_b][2]):  # 앵커 후보(그날 보고 잔액들)
            closings = {days[first_b][0]: candidate}
            running = candidate
            verified = 0
            ok = True
            for day, net, bals, _ in days[first_b + 1: last_b + 1]:
                running += net
                closings[day] = running
                if bals:
                    if any(abs(running - b) <= BALANCE_TOLERANCE for b in bals):
                        verified += 1
                    else:
                        ok = False
                        if first_fail is None:
                            first_fail = {"expected": running, "got": bals[-1], "day": day}
                        break
            if ok:
                status = "OK" if verified >= 1 else "OK_ANCHOR_ONLY"
                return {**base, "status": status, "closing": running, "closing_date": days[last_b][0],
                        "verified_days": verified, "unverified_rows": unverified, "day_closings": closings}
        fail = first_fail or {"expected": 0.0, "got": 0.0, "day": days[first_b][0]}
        return {**base, "status": "MISMATCH", "unverified_rows": unverified, **fail}

    def _log_import(self, file: str, added: int, skipped: int, status: str, detail: str | None) -> None:
        self.conn.execute(
            "INSERT INTO imports (file, rows_added, rows_skipped, status, detail, imported_at) VALUES (?, ?, ?, ?, ?, ?)",
            (file, added, skipped, status, detail, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    # ---------- 조회와 내보내기 ----------

    def balances(self) -> list[dict]:
        accounts = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT account FROM transactions ORDER BY account").fetchall()]
        result = []
        for account in accounts:
            check = self.verify_account(account)
            count = self.conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE account = ?", (account,)).fetchone()[0]
            result.append({
                "account": account,
                "last_date": check["closing_date"],
                "balance": check["closing"],
                "rows": count,
                "check": check["status"],
                "unverified_rows": check["unverified_rows"],
            })
        return result

    def imports_log(self, limit: int = 10) -> list[tuple]:
        return self.conn.execute(
            "SELECT imported_at, file, rows_added, rows_skipped, status FROM imports "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def transaction_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    def export_statement(self, path: str | Path) -> Path:
        """원장 전체를 기존 파이프라인이 읽는 표준 CSV로 내보낸다.

        잔액은 '검증된 일 단위 마감잔액'만 각 (계좌, 일자)의 마지막 행에 기록한다.
        파일마다 다른 같은 날 안의 행 순서가 하위 검증을 흔들지 않게 하기 위함이다.
        """
        rows = self.conn.execute(
            "SELECT account, date, description, withdrawal, deposit "
            "FROM transactions ORDER BY date, id").fetchall()
        frame = pd.DataFrame(rows, columns=["account", "date", "description", "withdrawal", "deposit"])
        frame["day"] = frame["date"].str[:10]
        closings: dict[tuple[str, str], float] = {}
        for account in frame["account"].unique():
            check = self.verify_account(str(account))
            for day, closing in check["day_closings"].items():
                closings[(str(account), day)] = closing
        balance_col: list = [pd.NA] * len(frame)
        last_row_idx = frame.groupby(["account", "day"]).tail(1).index
        for idx in last_row_idx:
            key = (str(frame.at[idx, "account"]), frame.at[idx, "day"])
            if key in closings:
                balance_col[idx] = closings[key]
        out = pd.DataFrame({
            "거래일시": frame["day"],
            "기재내용": frame["description"],
            "찾으신금액(출금)": frame["withdrawal"],
            "맡기신금액(입금)": frame["deposit"],
            "잔액": balance_col,
            "계좌": frame["account"],
        })
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(target, index=False, encoding="utf-8-sig")
        return target
