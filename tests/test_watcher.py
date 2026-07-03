"""inbox 감시 로직 테스트: 새 CSV는 크기가 안정된 뒤 정확히 한 번만 처리 대상이 된다."""

from watch_inbox import scan_ready


def test_new_csv_becomes_ready_once_after_stable(tmp_path):
    snapshots, processed = {}, {}
    assert scan_ready(tmp_path, snapshots, processed) == []

    target = tmp_path / "today.csv"
    target.write_text("date,desc,out,in\n2025-07-26,x,0,1\n", encoding="utf-8")

    # 첫 스캔: 발견은 되지만 아직 '안정' 상태가 아니므로 처리 대상 아님
    assert scan_ready(tmp_path, snapshots, processed) == []
    # 두 번째 스캔: 서명이 그대로면 처리 대상이 됨
    assert scan_ready(tmp_path, snapshots, processed) == [target]
    # 처리 완료로 표시하면 다시 등장하지 않음
    processed[target.name] = snapshots[target.name]
    assert scan_ready(tmp_path, snapshots, processed) == []


def test_modified_csv_triggers_again(tmp_path):
    snapshots, processed = {}, {}
    target = tmp_path / "today.csv"
    target.write_text("v1\n", encoding="utf-8")
    scan_ready(tmp_path, snapshots, processed)
    processed[target.name] = snapshots[target.name]

    target.write_text("v1\nv2 appended\n", encoding="utf-8")
    assert scan_ready(tmp_path, snapshots, processed) == []  # 변경 직후: 안정 대기
    assert scan_ready(tmp_path, snapshots, processed) == [target]  # 안정 후 재처리 대상
