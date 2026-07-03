"""단일 일일 흐름: inbox 폴더의 은행 파일을 승인받아 원장에 쌓고 보고서를 갱신한다.

1. 에이전트를 켠다 (python watch_inbox.py)
2. inbox/ 폴더의 새 파일(CSV/XLSX, 은행 무관)을 인식하고 "작업할까요?"라고 묻는다
3. 모르는 양식이면 AI가 컬럼 해석을 제안하고 사람이 승인한다 (헤더 이름만 AI에 전송)
4. 승인된 파일은 SQLite 원장(data/ledger.db)에 중복 없이 누적되고,
   계좌별 일 단위 크로스체크 [은행 보고 잔액 = 원장 재계산 잔액]를 통과해야 반영된다
   (불일치 시 반영 중단 + 상세 보고, 전체 롤백 — 은행이 진실이다)
5. 보고서는 원장 '전체'를 기준으로 다시 만들어지고, 처리된 파일은 archive/로 이동한다

--auto: 질문 없이 실행. 안전을 위해 '사람이 승인했고 크로스체크까지 통과한 양식' +
'파일 안에 계좌 컬럼이 있는 파일'만 처리한다 (승인 없는 자동 반영 금지 원칙).
계좌별 잔액 조회는 `python cf.py balances`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import tempfile
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)

from agents import format_reader
from agents.ledger import Ledger, LedgerMismatch
from agents.data_normalizer import DataNormalizerAgent
from agents.pipeline import prepare_analysis, review_items
from agents.security import MAX_CSV_BYTES
from main import run_adk_pipeline

STATEMENT_PATH = "data/ledger_statement.csv"


class InteractiveUnavailable(RuntimeError):
    pass


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError as exc:
        raise InteractiveUnavailable(
            "대화형 입력을 사용할 수 없습니다. 터미널에서 실행하거나 --auto를 사용하세요."
        ) from exc


def scan_ready(folder: Path, snapshots: dict, processed: dict) -> list[Path]:
    """새로 오거나 바뀐 뒤 크기가 안정된 CSV/XLSX 파일 목록을 돌려준다."""
    ready: list[Path] = []
    files = sorted(list(folder.glob("*.csv")) + list(folder.glob("*.xlsx")))
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature = (stat.st_mtime_ns, stat.st_size)
        if snapshots.get(path.name) == signature and processed.get(path.name) != signature:
            ready.append(path)
        snapshots[path.name] = signature
    return ready


def resolve_mapping_flow(raw: pd.DataFrame, ledger: Ledger, source: str, auto: bool) -> tuple[dict | None, str]:
    """저장된 양식 → 내장 별칭 → AI 제안 → 수동 순서로 컬럼 매핑을 정한다."""
    headers = [str(c) for c in raw.columns]
    signature = format_reader.format_signature(headers)
    saved = ledger.format_record(signature)

    if auto:
        if saved and saved["verified"]:
            return format_reader.resolve_mapping(json.loads(saved["mapping"]), headers), signature
        print("  --auto는 검증 완료된 양식만 처리합니다. 먼저 일반 모드로 한 번 승인해 주세요.")
        return None, signature

    if saved:
        mapping = format_reader.resolve_mapping(json.loads(saved["mapping"]), headers)
        if saved["verified"] or format_reader.confirm_mapping(mapping, f"{source} · 저장된 양식"):
            return mapping, signature
    if not raw.attrs.get("headerless"):
        mapping = format_reader.alias_mapping(headers)
        if mapping:
            return mapping, signature
        mapping = format_reader.llm_suggest_mapping(headers)
        if mapping and format_reader.confirm_mapping(mapping, f"{source} · AI 제안"):
            return mapping, signature
    else:
        print("  헤더 행이 없는 파일로 보입니다. (개인정보 보호를 위해 AI 해석 없이 수동 매핑으로 진행)")
    return format_reader.interactive_mapping(raw), signature


def determine_account(raw: pd.DataFrame, mapping: dict, ledger: Ledger, signature: str,
                      source: str, auto: bool) -> str | None:
    column = mapping.get("account")
    if column and column in raw.columns and not raw[column].fillna("").astype(str).str.strip().eq("").all():
        return None  # 파일의 계좌 컬럼을 그대로 사용
    if auto:
        print("  --auto는 파일에 계좌 컬럼이 있는 경우만 처리합니다 (계좌 오귀속 방지).")
        return "__SKIP__"
    saved = ledger.format_record(signature)
    default = (saved and saved["last_account"]) or Path(source).stem
    entered = ask(f"이 파일의 은행/계좌 이름 [{default}]: ")
    return entered or default


def ingest_file(path: Path, ledger: Ledger, auto: bool) -> bool:
    """파일 하나를 해석·검증해 원장에 반영한다. 성공 시 True."""
    if path.stat().st_size > MAX_CSV_BYTES:
        print(f"  거부: {path.name} 크기가 10 MB 제한을 넘습니다.")
        return False
    raw = format_reader.read_table(path)
    mapping, signature = resolve_mapping_flow(raw, ledger, path.name, auto)
    if not mapping:
        print(f"  건너뜀: {path.name} — 컬럼 매핑이 확정되지 않았습니다.")
        return False
    account_label = determine_account(raw, mapping, ledger, signature, path.name, auto)
    if account_label == "__SKIP__":
        return False

    canonical = format_reader.to_canonical(raw, mapping, account_label)
    fd, temp_path = tempfile.mkstemp(dir="data", suffix=".csv", prefix="incoming_")
    os.close(fd)
    try:
        canonical.to_csv(temp_path, index=False, encoding="utf-8-sig")
        validation = DataNormalizerAgent().normalize_with_validation(temp_path)
    finally:
        Path(temp_path).unlink(missing_ok=True)

    errors = [i for i in validation.issues if i.level == "error"]
    if errors:
        print(f"  주의: 날짜/금액을 해석할 수 없는 {len(errors)}개 행이 제외됐습니다.")

    # 잔액 정보가 전혀 없는 계좌는 수학적 검증이 불가능하다 — 명시적 확인을 받는다.
    no_balance_accounts = [
        str(account) for account, group in validation.data.groupby("Account")
        if group["Balance"].isna().all()
    ]
    if no_balance_accounts:
        if auto:
            print(f"  --auto 건너뜀: 잔액 컬럼이 없어 크로스체크가 불가능합니다 ({', '.join(no_balance_accounts)}).")
            return False
        answer = ask(
            f"  이 파일에는 잔액 정보가 없어 [{', '.join(no_balance_accounts)}] 계좌를 검증할 수 없습니다. "
            "그래도 반영할까요? [y/N]: ").lower()
        if answer not in {"y", "yes"}:
            return False

    try:
        result = ledger.ingest(validation.data, path.name)
    except LedgerMismatch as exc:
        print(f"  반영 거부: {exc}")
        return False

    notes = []
    for check in result["accounts"]:
        note = f"{check['account']}: {check['status']}"
        if check["closing"] is not None:
            note += f" (마감 {check['closing']:,.0f}원)"
        if check["unverified_rows"]:
            note += f" · 미검증 {check['unverified_rows']}행"
        notes.append(note)
    print(f"  원장 반영: +{result['added']}건, 중복 {result['skipped']}건 제외. 크로스체크 [{'; '.join(notes)}]")

    verified = bool(result["accounts"]) and all(c["status"] == "OK" for c in result["accounts"])
    ledger.save_format(signature, json.dumps(mapping, ensure_ascii=False), account_label, verified)
    return True


def archive(path: Path, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = archive_dir / f"{stamp}_{path.name}"
    shutil.move(str(path), target)
    return target


def rebuild_report(args) -> None:
    ledger = Ledger()
    try:
        if ledger.transaction_count() == 0:
            print("원장이 비어 있어 보고서를 만들지 않았습니다.")
            return
        statement = ledger.export_statement(STATEMENT_PATH)
    finally:
        ledger.close()
    if not args.auto:
        _, store, categorized, patterns = prepare_analysis(str(statement))
        pending = any(
            store.classification(desc) is None
            for desc in categorized[categorized["ClassificationStatus"] == "needs_review"]["Description"].unique()
        ) or any(p.pattern_id not in store.data["recurrences"] for p in patterns)
        if pending:
            review_items(categorized, patterns, store)
    result = asyncio.run(run_adk_pipeline(str(statement), args.output))
    print(f"원장 전체 기준으로 보고서를 갱신했습니다: {result['report']} (기준일 {result['as_of_date']})")
    if not args.no_open:
        webbrowser.open(Path(result["report"]).as_uri())


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Approve inbox bank files into the SQLite ledger and rebuild the CFO report.")
    parser.add_argument("--inbox", default="inbox", help="Watched folder inside this workspace")
    parser.add_argument("--archive", default="archive", help="Folder for processed files")
    parser.add_argument("--output", default="output/cfo_control_tower.html", help="HTML report path")
    parser.add_argument("--poll", type=float, default=2.0, help="Seconds between folder scans")
    parser.add_argument("--iterations", type=int, default=0, help="Stop after N scans (0 = run until Ctrl+C); for tests")
    parser.add_argument("--auto", action="store_true", help="No questions: verified formats with account columns only")
    parser.add_argument("--no-open", action="store_true", help="Do not open the refreshed report in the browser")
    args = parser.parse_args()

    inbox = Path(args.inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    workspace = Path.cwd().resolve()
    if workspace not in inbox.resolve().parents and inbox.resolve() != workspace:
        pass  # inbox는 프로젝트 안에 있어야 함 — 아래 파일 단위 검사에서 걸러짐

    snapshots: dict = {}
    processed: dict = {}
    print(f"Watching {inbox.resolve()} for bank files (.csv/.xlsx). Press Ctrl+C to stop.")
    pending_now = len(list(inbox.glob("*.csv")) + list(inbox.glob("*.xlsx")))
    if pending_now:
        print(f"Found {pending_now} file(s) already in the inbox; they will be offered for processing.")

    scans = 0
    try:
        while True:
            scans += 1
            batch_success = False
            for path in scan_ready(inbox, snapshots, processed):
                signature = snapshots[path.name]
                if path.resolve().parent != inbox.resolve() or workspace not in path.resolve().parents:
                    processed[path.name] = signature
                    print(f"경고: {path.name} 은(는) 작업 폴더 밖을 가리켜 무시합니다.")
                    continue
                print(f"\nNew bank file detected: {path.name}")
                ok = False
                try:
                    if not args.auto:
                        answer = ask(f"Process {path.name} into the ledger? [y/N]: ").lower()
                        if answer not in {"y", "yes"}:
                            print("  inbox에 그대로 둡니다. 파일을 수정하거나 재시작하면 다시 묻습니다.")
                            processed[path.name] = signature
                            continue
                    ledger = Ledger()
                    try:
                        ok = ingest_file(path, ledger, args.auto)
                    finally:
                        ledger.close()
                    if ok:
                        filed = archive(path, Path(args.archive))
                        print(f"  보관 완료: {filed}")
                        batch_success = True
                except (KeyboardInterrupt, InteractiveUnavailable):
                    raise
                except PermissionError:
                    print(f"  실패: {path.name} 이(가) 다른 프로그램(엑셀 등)에서 열려 있습니다. 닫고 파일을 다시 저장하면 재시도합니다.")
                    continue  # processed 표시 안 함 → 파일 저장(변경) 시 재시도
                except Exception as exc:  # 파일 1건의 실패가 감시자를 죽이지 않게 한다
                    print(f"  실패({type(exc).__name__}): {exc}")
                processed[path.name] = signature
            if batch_success:
                try:
                    rebuild_report(args)
                except (KeyboardInterrupt, InteractiveUnavailable):
                    raise
                except Exception as exc:
                    print(f"보고서 갱신 실패({type(exc).__name__}): {exc} — 원장 반영은 완료된 상태이며 감시는 계속됩니다.")
            if args.iterations and scans >= args.iterations:
                return 0
            time.sleep(args.poll)
    except InteractiveUnavailable as exc:
        print(f"\n{exc}")
        return 1
    except KeyboardInterrupt:
        print("\nWatcher stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
