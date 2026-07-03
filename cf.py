"""원장 조회 명령. 대시보드를 어지럽히지 않고, 필요할 때 즉시 계좌별 자료를 꺼낸다.

사용법:
    python cf.py balances   # 계좌별 최신 잔액 + 크로스체크 상태 + 총액
    python cf.py imports    # 최근 반영 이력
API 키 불필요, LLM 미사용 — 원장 DB를 그대로 읽는다.
"""

from __future__ import annotations

import argparse
import os
import unicodedata
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)

from agents.ledger import Ledger


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def show_balances(ledger: Ledger) -> None:
    rows = ledger.balances()
    if not rows:
        print("Ledger is empty. Drop a bank file into inbox/ while watch_inbox.py is running.")
        return
    print(f"{pad('Account', 30)} {pad('As of', 12)} {'Balance (KRW 000)':>18} {'Rows':>6}  Check")
    print("-" * 84)
    total = 0.0
    unknown = 0
    for row in rows:
        if row["balance"] is None:
            balance_text = f"{'?':>17} "
            unknown += 1
        else:
            balance_text = f"{row['balance'] / 1000:>17,.0f}K"
            total += row["balance"]
        check = row["check"]
        if row["unverified_rows"]:
            check += f" (미검증 {row['unverified_rows']}행)"
        print(f"{pad(row['account'], 30)} {pad(row['last_date'] or '-', 12)} {balance_text} {row['rows']:>6}  {check}")
    print("-" * 84)
    label = "TOTAL" if not unknown else f"TOTAL (잔액 미상 {unknown}개 계좌 제외)"
    print(f"{pad(label, 43)} {total / 1000:>17,.0f}K")


def show_imports(ledger: Ledger) -> None:
    rows = ledger.imports_log(limit=15)
    if not rows:
        print("No imports recorded yet.")
        return
    print(f"{pad('Imported at', 20)} {pad('File', 34)} {'Added':>6} {'Dup':>5}  Status")
    print("-" * 84)
    for imported_at, file, added, skipped, status in rows:
        print(f"{pad(imported_at, 20)} {pad(file[:33], 34)} {added:>6} {skipped:>5}  {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Query the cash ledger (no API key needed).")
    parser.add_argument("command", choices=["balances", "imports"], help="What to show")
    args = parser.parse_args()
    ledger = Ledger()
    try:
        if args.command == "balances":
            show_balances(ledger)
        else:
            show_imports(ledger)
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
