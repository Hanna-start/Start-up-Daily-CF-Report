"""원장·양식 해석기 테스트: 중복 제거, 크로스체크 롤백, 다계좌 합산, 양식 기억, XLSX 해석."""

import pandas as pd
import pytest

from agents.data_normalizer import DataNormalizerAgent
from agents.format_reader import alias_mapping, format_signature, read_table, to_canonical
from agents.ledger import Ledger, LedgerMismatch


def make_frame(rows):
    frame = pd.DataFrame(rows, columns=["Date", "Description", "Withdrawal", "Deposit", "Balance", "Account"])
    frame["Date"] = pd.to_datetime(frame["Date"])
    frame["SourceRow"] = range(2, len(frame) + 2)
    return frame


GOOD_ROWS = [
    ("2025-07-01", "기초잔액", 0.0, 1000000.0, 1000000.0, "우리은행"),
    ("2025-07-02", "매출 입금", 0.0, 500000.0, 1500000.0, "우리은행"),
    ("2025-07-03", "부품 구매", 300000.0, 0.0, 1200000.0, "우리은행"),
]


def test_ingest_dedupes_identical_rows(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    first = ledger.ingest(make_frame(GOOD_ROWS), "bank_a.csv")
    assert first["added"] == 3 and first["skipped"] == 0
    second = ledger.ingest(make_frame(GOOD_ROWS), "bank_a.csv")
    assert second["added"] == 0 and second["skipped"] == 3
    assert ledger.transaction_count() == 3
    ledger.close()


def test_crosscheck_mismatch_rolls_back(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.ingest(make_frame(GOOD_ROWS), "bank_a.csv")
    corrupted = make_frame([
        ("2025-07-04", "지출", 100000.0, 0.0, 2000000.0, "우리은행"),  # 흐름상 1,100,000이어야 함
    ])
    with pytest.raises(LedgerMismatch):
        ledger.ingest(corrupted, "bank_a_day2.csv")
    assert ledger.transaction_count() == 3  # 롤백으로 원장 오염 없음
    statuses = [row[4] for row in ledger.imports_log()]
    assert "REJECTED" in statuses
    ledger.close()


def test_multi_account_totals_and_statement_export(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.ingest(make_frame(GOOD_ROWS), "woori.csv")
    ledger.ingest(make_frame([
        ("2025-07-01", "기초잔액", 0.0, 800000.0, 800000.0, "신한은행"),
        ("2025-07-03", "급여 지급", 200000.0, 0.0, 600000.0, "신한은행"),
    ]), "shinhan.csv")

    balances = {row["account"]: row for row in ledger.balances()}
    assert balances["우리은행"]["balance"] == 1200000.0
    assert balances["신한은행"]["balance"] == 600000.0
    assert all(row["check"] == "OK" for row in balances.values())

    statement = ledger.export_statement(tmp_path / "statement.csv")
    validation = DataNormalizerAgent().normalize_with_validation(statement)
    assert validation.balance_reliable
    assert validation.current_cash == 1800000.0  # 계좌별 최신 잔액의 합
    ledger.close()


def test_format_memory_roundtrip(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    signature = format_signature(["거래일자", "내용", "출금", "입금", "잔액"])
    ledger.save_format(signature, '{"date": "거래일자"}', "우리은행 주계좌", verified=False)
    record = ledger.format_record(signature)
    assert record["mapping"] == '{"date": "거래일자"}'
    assert record["last_account"] == "우리은행 주계좌"
    assert record["verified"] is False
    # 검증 통과 후에는 verified가 True로 승격되고, 이후 미검증 저장이 이를 끌어내리지 못한다
    ledger.save_format(signature, '{"date": "거래일자"}', "우리은행 주계좌", verified=True)
    assert ledger.format_record(signature)["verified"] is True
    ledger.save_format(signature, '{"date": "거래일자"}', "우리은행 주계좌", verified=False)
    assert ledger.format_record(signature)["verified"] is True
    ledger.close()


def test_alias_mapping_handles_known_korean_headers():
    headers = ["거래일시", "기재내용", "찾으신금액(출금)", "맡기신금액(입금)", "잔액", "계좌"]
    mapping = alias_mapping(headers)
    assert mapping is not None
    assert mapping["date"] == "거래일시" and mapping["withdrawal"] == "찾으신금액(출금)"


def test_reversed_order_bank_file_passes_crosscheck(tmp_path):
    """최신순 정렬 + 시각 없는 파일도 일 단위 검증으로 정상 통과해야 한다 (리뷰 회귀)."""
    rows = [
        ("2025-07-02", "오후 입금", 0.0, 100000.0, 3200000.0, "국민은행"),
        ("2025-07-02", "식대", 50000.0, 0.0, 3100000.0, "국민은행"),
        ("2025-07-02", "오전 입금", 0.0, 150000.0, 3150000.0, "국민은행"),
        ("2025-07-01", "이월잔액", 0.0, 3000000.0, 3000000.0, "국민은행"),
    ]
    ledger = Ledger(tmp_path / "ledger.db")
    result = ledger.ingest(make_frame(rows), "kb_desc_order.csv")
    check = result["accounts"][0]
    assert check["status"] == "OK"
    assert check["closing"] == 3200000.0
    ledger.close()


def test_same_day_backfill_is_accepted(tmp_path):
    """같은 날의 이른 거래를 나중 파일로 보완해도 거부되지 않아야 한다 (리뷰 회귀)."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.ingest(make_frame([
        ("2025-07-01", "입금", 0.0, 1000000.0, 11000000.0, "주계좌"),
    ]), "first.csv")
    result = ledger.ingest(make_frame([
        ("2025-07-01", "아침 출금", 500000.0, 0.0, 10000000.0, "주계좌"),
    ]), "backfill.csv")
    assert result["accounts"][0]["status"] in {"OK", "OK_ANCHOR_ONLY"}
    assert ledger.transaction_count() == 2
    ledger.close()


def test_legitimate_same_amount_duplicates_are_kept(tmp_path):
    """잔액 없는 파일에서 같은 날 동일 금액·적요의 정당한 거래 2건은 모두 반영돼야 한다 (리뷰 회귀)."""
    rows = [
        ("2025-07-02", "점심식대", 5000.0, 0.0, float("nan"), "법인카드"),
        ("2025-07-02", "점심식대", 5000.0, 0.0, float("nan"), "법인카드"),
    ]
    ledger = Ledger(tmp_path / "ledger.db")
    result = ledger.ingest(make_frame(rows), "card.csv")
    assert result["added"] == 2 and result["skipped"] == 0
    assert result["accounts"][0]["status"] == "NO_BALANCE"
    # 같은 파일을 다시 넣으면 두 건 모두 중복으로 걸러진다
    again = ledger.ingest(make_frame(rows), "card.csv")
    assert again["added"] == 0 and again["skipped"] == 2
    ledger.close()


def test_blank_balance_preserved_as_na_not_zero(tmp_path):
    """빈 잔액 칸이 0원으로 둔갑하면 안 된다 (리뷰 회귀: 유령 0 잔액)."""
    csv = tmp_path / "no_balance.csv"
    csv.write_text(
        "거래일시,기재내용,찾으신금액(출금),맡기신금액(입금),잔액,계좌\n"
        "2025-07-01,입금,0,8000000,,간편장부\n"
        "2025-07-02,지출,2000000,0,,간편장부\n",
        encoding="utf-8-sig",
    )
    validation = DataNormalizerAgent().normalize_with_validation(csv)
    assert validation.data["Balance"].isna().all()
    assert validation.current_cash == 6000000.0  # 순입출금 누계 폴백
    assert any(i.code == "NO_BALANCE" for i in validation.issues)


def test_continuity_accumulates_flows_of_gap_rows(tmp_path):
    """잔액 행 사이에 낀 무잔액 행의 입출금도 연속성 검사에 포함돼야 한다 (리뷰 회귀)."""
    csv = tmp_path / "gap.csv"
    csv.write_text(
        "거래일시,기재내용,찾으신금액(출금),맡기신금액(입금),잔액,계좌\n"
        "2025-07-01,입금,0,1000000,1000000,주계좌\n"
        "2025-07-02,중간지출,200000,0,,주계좌\n"
        "2025-07-03,지출,0,0,,주계좌\n"
        "2025-07-04,입금,0,100000,900000,주계좌\n",
        encoding="utf-8-sig",
    )
    validation = DataNormalizerAgent().normalize_with_validation(csv)
    assert validation.balance_reliable
    assert validation.current_cash == 900000.0


def test_export_writes_one_verified_closing_per_day(tmp_path):
    """원장 내보내기는 (계좌, 일자)당 검증된 마감잔액 하나만 기록해야 한다."""
    rows = [
        ("2025-07-01", "이월", 0.0, 3000000.0, 3000000.0, "국민은행"),
        ("2025-07-02", "오전 입금", 0.0, 150000.0, 3150000.0, "국민은행"),
        ("2025-07-02", "식대", 50000.0, 0.0, 3100000.0, "국민은행"),
        ("2025-07-02", "오후 입금", 0.0, 100000.0, 3200000.0, "국민은행"),
    ]
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.ingest(make_frame(rows), "kb.csv")
    statement = ledger.export_statement(tmp_path / "statement.csv")
    validation = DataNormalizerAgent().normalize_with_validation(statement)
    per_day = validation.data.dropna(subset=["Balance"]).groupby(validation.data["Date"].dt.date).size()
    assert (per_day == 1).all()
    assert validation.balance_reliable
    assert validation.current_cash == 3200000.0
    ledger.close()


def test_read_table_xlsx_with_preamble_and_canonical_conversion(tmp_path):
    rows = [
        ["조회기간: 2025-07-01 ~ 2025-07-03", None, None, None, None],
        ["거래일자", "적요", "출금액", "입금액", "잔액"],
        ["2025-07-01", "기초잔액", "0", "1,000,000", "1,000,000"],
        ["2025-07-02", "매출", "0", "500,000", "1,500,000"],
    ]
    path = tmp_path / "shinhan.xlsx"
    pd.DataFrame(rows).to_excel(path, header=False, index=False)

    raw = read_table(path)
    assert list(raw.columns) == ["거래일자", "적요", "출금액", "입금액", "잔액"]
    assert len(raw) == 2

    mapping = alias_mapping(list(raw.columns))
    assert mapping is not None
    canonical = to_canonical(raw, mapping, "신한은행 급여계좌")
    assert set(["거래일시", "기재내용", "찾으신금액(출금)", "맡기신금액(입금)", "잔액", "계좌"]).issubset(canonical.columns)
    assert (canonical["계좌"] == "신한은행 급여계좌").all()
