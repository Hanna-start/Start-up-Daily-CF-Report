"""검증 가능한 현금 예측 엔진. LLM은 금액 계산에 관여하지 않는다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from .recurrence import RecurringPattern


SCENARIOS = {
    "conservative": {"label": "Downside Case", "inflow": 0.85, "outflow": 1.15},
    "base": {"label": "Base Case", "inflow": 1.00, "outflow": 1.00},
    "optimistic": {"label": "Upside Case", "inflow": 1.10, "outflow": 0.95},
}


@dataclass
class ScenarioSummary:
    key: str
    label: str
    ending_cash_90d: float
    ending_cash_12m: float
    safe_cash_date: str | None
    exhaustion_date: str | None
    runway_months: float | None


@dataclass
class ForecastResult:
    as_of_date: str
    current_cash: float
    cash_basis: str
    monthly_operating_burn: float
    monthly_operating_inflow: float
    safe_cash_reserve: float
    daily: pd.DataFrame
    monthly: pd.DataFrame
    summaries: list[ScenarioSummary]
    assumptions: list[str]
    confidence: str
    unclassified_count: int = 0
    unclassified_outflow: float = 0.0
    unclassified_inflow: float = 0.0

    def summary_dicts(self) -> list[dict[str, Any]]:
        return [vars(item) for item in self.summaries]


class CashForecastAgent:
    def forecast(self, df: pd.DataFrame, current_cash: float, cash_basis: str, patterns: list[RecurringPattern]) -> ForecastResult:
        if df.empty:
            raise ValueError("예측할 거래가 없습니다.")
        as_of = pd.Timestamp(df["Date"].max()).normalize()
        history_start = pd.Timestamp(df["Date"].min()).normalize()
        observed_days = max(1, (as_of - history_start).days + 1)
        observed_months = max(1.0, observed_days / 30.4375)

        confidence_series = pd.to_numeric(
            df.get("ClassificationConfidence", 0), errors="coerce"
        ).fillna(0)
        trusted_mask = (
            (df.get("ClassificationStatus", "") == "approved")
            | (confidence_series >= 0.75)
        )
        approved_or_confident = df[trusted_mask].copy()
        non_cashflow = {"transfer", "opening_balance"}
        unclassified = df[
            ~trusted_mask & ~df["CategoryCode"].isin(non_cashflow)
        ]
        unclassified_outflow = float(unclassified["Withdrawal"].sum())
        unclassified_inflow = float(unclassified["Deposit"].sum())
        unclassified_count = int(len(unclassified))
        op_out = approved_or_confident[approved_or_confident["CategoryCode"].isin(["operating_fixed", "operating_variable"])]
        op_in = approved_or_confident[approved_or_confident["CategoryCode"] == "operating_inflow"]
        monthly_burn = float(
            (op_out["Withdrawal"].sum() + unclassified_outflow) / observed_months
        )
        monthly_inflow = float(
            (op_in["Deposit"].sum() + unclassified_inflow) / observed_months
        )
        safe_reserve = monthly_burn * 6

        approved = [p for p in patterns if p.approved]
        daily_dates = pd.date_range(as_of + pd.Timedelta(days=1), periods=365, freq="D")
        output_daily: list[dict[str, Any]] = []
        output_monthly: list[dict[str, Any]] = []
        summaries: list[ScenarioSummary] = []

        # 승인된 반복 거래를 날짜에 배치하고, 나머지는 과거 평균 기반 통계 추정으로 균등 배분한다.
        approved_monthly_in = sum(p.amount for p in approved if p.direction == "inflow")
        approved_monthly_out = sum(p.amount for p in approved if p.direction == "outflow")
        residual_in_daily = max(0.0, monthly_inflow - approved_monthly_in) / 30.4375
        residual_out_daily = max(0.0, monthly_burn - approved_monthly_out) / 30.4375

        for scenario_key, multipliers in SCENARIOS.items():
            cash = float(current_cash)
            safe_date: str | None = None
            exhaustion_date: str | None = None
            scenario_rows = []
            for day in daily_dates:
                scheduled_in = sum(p.amount for p in approved if p.direction == "inflow" and min(p.day_of_month, day.days_in_month) == day.day)
                scheduled_out = sum(p.amount for p in approved if p.direction == "outflow" and min(p.day_of_month, day.days_in_month) == day.day)
                inflow = (scheduled_in + residual_in_daily) * multipliers["inflow"]
                outflow = (scheduled_out + residual_out_daily) * multipliers["outflow"]
                cash += inflow - outflow
                if safe_date is None and cash < safe_reserve:
                    safe_date = day.date().isoformat()
                if exhaustion_date is None and cash <= 0:
                    exhaustion_date = day.date().isoformat()
                scenario_rows.append({"Date": day, "Scenario": scenario_key, "Inflow": inflow, "Outflow": outflow, "EndingCash": cash})
            output_daily.extend(scenario_rows[:90])
            scenario_df = pd.DataFrame(scenario_rows)
            monthly_df = scenario_df.assign(Month=scenario_df["Date"].dt.to_period("M").astype(str)).groupby("Month", as_index=False).agg({"Inflow": "sum", "Outflow": "sum", "EndingCash": "last"})
            monthly_df.insert(1, "Scenario", scenario_key)
            output_monthly.extend(monthly_df.head(12).to_dict("records"))

            monthly_net_burn = monthly_burn * multipliers["outflow"] - monthly_inflow * multipliers["inflow"]
            runway = round(current_cash / monthly_net_burn, 1) if monthly_net_burn > 0 else None
            summaries.append(ScenarioSummary(
                scenario_key, multipliers["label"], float(scenario_rows[89]["EndingCash"]),
                float(monthly_df.iloc[min(11, len(monthly_df) - 1)]["EndingCash"]), safe_date, exhaustion_date, runway,
            ))

        assumptions = [
            "Current cash is based on the latest available bank balance after reconciliation.",
            "The minimum cash reserve equals six months of normalized operating expenses.",
            "Approved recurring items are scheduled on their expected payment or receipt dates.",
            "All other operating cash flows are statistically estimated from historical daily averages.",
            "Downside and Upside Cases apply separate sensitivity factors to inflows and outflows.",
            "Bank transaction data alone cannot confirm future contracts, taxes, debt maturities, or new sales.",
        ]
        if unclassified_count:
            assumptions.append(
                f"{unclassified_count} unclassified transactions (outflow KRW "
                f"{unclassified_outflow:,.0f}, inflow KRW {unclassified_inflow:,.0f}) "
                "were conservatively included in operating cash flows. Run with "
                "--review to classify them."
            )
        confidence = "High" if len(approved) >= 3 and observed_months >= 6 else "Moderate" if observed_months >= 3 else "Low"
        total_flow = float(df["Withdrawal"].sum() + df["Deposit"].sum())
        if (
            total_flow > 0
            and (unclassified_outflow + unclassified_inflow) / total_flow > 0.3
        ):
            confidence = "Low"
        return ForecastResult(
            as_of.date().isoformat(),
            float(current_cash),
            cash_basis,
            monthly_burn,
            monthly_inflow,
            safe_reserve,
            pd.DataFrame(output_daily),
            pd.DataFrame(output_monthly),
            summaries,
            assumptions,
            confidence,
            unclassified_count=unclassified_count,
            unclassified_outflow=unclassified_outflow,
            unclassified_inflow=unclassified_inflow,
        )
