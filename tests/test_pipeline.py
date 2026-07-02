from pathlib import Path

import pandas as pd

from agents.categorizer import ApprovalStore, CategorizerAgent
from agents.data_normalizer import DataNormalizerAgent
from agents.forecaster import CashForecastAgent
from agents.recurrence import RecurrenceDetectorAgent
from agents.security import MAX_CSV_BYTES, validate_local_csv
from cfo_adk.agent import SKILL_DIR, build_root_agent
from google.adk.skills import load_skill_from_dir


ROOT = Path(__file__).parents[1]


def test_sample_balance_is_reliable():
    result = DataNormalizerAgent().normalize_with_validation(ROOT / "sample_bank_data.csv")
    assert result.balance_reliable is True
    assert result.current_cash == 140_000_000
    assert not result.has_errors


def test_detects_monthly_patterns_without_auto_approval(tmp_path):
    normalized = DataNormalizerAgent().normalize_with_validation(ROOT / "sample_bank_data.csv")
    store = ApprovalStore(tmp_path / "approvals.json")
    categorized = CategorizerAgent(store).categorize(normalized.data)
    patterns = RecurrenceDetectorAgent(store).detect(categorized)
    assert len(patterns) >= 5
    assert all(not item.approved for item in patterns)


def test_approval_changes_pattern_state(tmp_path):
    normalized = DataNormalizerAgent().normalize_with_validation(ROOT / "sample_bank_data.csv")
    store = ApprovalStore(tmp_path / "approvals.json")
    categorized = CategorizerAgent(store).categorize(normalized.data)
    detector = RecurrenceDetectorAgent(store)
    first = detector.detect(categorized)[0]
    store.set_recurrence(first.pattern_id, True)
    assert any(p.pattern_id == first.pattern_id and p.approved for p in detector.detect(categorized))


def test_forecast_has_90_daily_and_12_monthly_rows_per_scenario(tmp_path):
    normalized = DataNormalizerAgent().normalize_with_validation(ROOT / "sample_bank_data.csv")
    store = ApprovalStore(tmp_path / "approvals.json")
    categorized = CategorizerAgent(store).categorize(normalized.data)
    patterns = RecurrenceDetectorAgent(store).detect(categorized)
    forecast = CashForecastAgent().forecast(categorized, normalized.current_cash, normalized.cash_basis, patterns)
    assert len(forecast.daily) == 90 * 3
    assert len(forecast.monthly) == 12 * 3
    assert {x.key for x in forecast.summaries} == {"conservative", "base", "optimistic"}


def test_rejects_non_csv(tmp_path):
    path = tmp_path / "secret.xlsx"
    path.write_text("not a csv", encoding="utf-8")
    try:
        DataNormalizerAgent().normalize_with_validation(path)
    except ValueError as exc:
        assert "CSV" in str(exc)
    else:
        raise AssertionError("non-CSV input should be rejected")


def test_security_rejects_csv_outside_workspace(tmp_path):
    path = tmp_path / "bank.csv"
    path.write_text("date,description,withdrawal,deposit\n", encoding="utf-8")
    try:
        validate_local_csv(path, allowed_root=ROOT)
    except ValueError as exc:
        assert "workspace" in str(exc)
    else:
        raise AssertionError("CSV outside the workspace should be rejected")


def test_adk_workflow_contains_real_processing_agents():
    root = build_root_agent(use_llm=False)
    assert [agent.name for agent in root.sub_agents] == [
        "DataValidatorAgent",
        "TransactionAnalystAgent",
        "CashForecastAgent",
        "CFOBriefingAgent",
        "ReportBuilderAgent",
    ]


def test_cash_runway_agent_skill_loads():
    skill = load_skill_from_dir(SKILL_DIR)
    assert skill.frontmatter.name == "analyze-cash-runway"
