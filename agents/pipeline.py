"""Reusable deterministic finance pipeline used by ADK workflow agents."""

from __future__ import annotations

from pathlib import Path

from .categorizer import ApprovalStore, CategorizerAgent
from .cfo_analyst import CFOAnalystAgent
from .data_normalizer import DataNormalizerAgent
from .forecaster import CashForecastAgent
from .recurrence import RecurrenceDetectorAgent
from .security import validate_local_csv, validate_output_path


def prepare_analysis(csv_path: str, approval_store: ApprovalStore | None = None):
    safe_csv = validate_local_csv(csv_path)
    validation = DataNormalizerAgent().normalize_with_validation(safe_csv)
    store = approval_store or ApprovalStore()
    categorized = CategorizerAgent(store).categorize(validation.data)
    patterns = RecurrenceDetectorAgent(store).detect(categorized)
    return validation, store, categorized, patterns


def review_items(categorized, patterns, store: ApprovalStore) -> None:
    uncertain = categorized[categorized["ClassificationStatus"] == "needs_review"]
    for description in uncertain["Description"].drop_duplicates():
        row = uncertain[uncertain["Description"] == description].iloc[0]
        print(f"\n[Classification approval] {description}")
        print(f"Suggested: {row['Category']} / Reason: {row['ClassificationReason']}")
        if input("Approve this classification? [y/N]: ").strip().lower() in {"y", "yes"}:
            store.set_classification(description, row["CategoryCode"], True)
    for pattern in patterns:
        if pattern.approved:
            continue
        print(f"\n[Recurring item approval] {pattern.description}")
        print(f"Day {pattern.day_of_month} monthly · {pattern.direction} · {pattern.amount:,.0f} · confidence {pattern.confidence:.0%}")
        approved = input("Include this recurring item in the forecast? [y/N]: ").strip().lower() in {"y", "yes"}
        store.set_recurrence(pattern.pattern_id, approved)


def compute_forecast(csv_path: str, review: bool = False):
    validation, store, categorized, patterns = prepare_analysis(csv_path)
    if review:
        review_items(categorized, patterns, store)
        validation, store, categorized, patterns = prepare_analysis(csv_path, store)
    forecast = CashForecastAgent().forecast(
        categorized, validation.current_cash or 0.0, validation.cash_basis, patterns
    )
    return validation, categorized, patterns, forecast


def run_pipeline(
    csv_path: str,
    output_path: str = "output/cfo_control_tower.html",
    review: bool = False,
    briefing: str | None = None,
) -> dict:
    safe_output = validate_output_path(output_path)
    validation, categorized, patterns, forecast = compute_forecast(csv_path, review=review)
    report = CFOAnalystAgent().analyze_and_report(
        forecast, validation, patterns, safe_output, briefing=briefing
    )
    return {
        "report": str(Path(report).resolve()),
        "as_of_date": forecast.as_of_date,
        "current_cash": forecast.current_cash,
        "cash_basis": forecast.cash_basis,
        "balance_reliable": validation.balance_reliable,
        "validation_issue_count": len(validation.issues),
        "recurring_candidates": [p.as_dict() for p in patterns],
        "scenarios": forecast.summary_dicts(),
        "confidence": forecast.confidence,
    }
