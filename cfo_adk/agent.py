"""ADK-first CFO Cash Control Tower workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset
from google.genai import types

from agents.pipeline import compute_forecast, prepare_analysis, run_pipeline


MODEL = "gemini-2.5-flash"
SKILL_DIR = Path(__file__).parent / "skills" / "analyze-cash-runway"


def _event(agent: BaseAgent, text: str, state_delta: dict) -> Event:
    return Event(
        author=agent.name,
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        actions=EventActions(stateDelta=state_delta),
    )


class DataValidatorAgent(BaseAgent):
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        csv_path = str(ctx.session.state["csv_path"])
        validation, _, _, _ = prepare_analysis(csv_path)
        summary = {
            "as_of_date": validation.data["Date"].max().date().isoformat(),
            "transaction_count": len(validation.data),
            "current_cash": validation.current_cash,
            "balance_reliable": validation.balance_reliable,
            "issue_count": len(validation.issues),
        }
        yield _event(self, "Bank CSV validated and balances reconciled.", {"validation_summary": summary})


class TransactionAnalystAgent(BaseAgent):
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        csv_path = str(ctx.session.state["csv_path"])
        validation, _, categorized, patterns = prepare_analysis(csv_path)
        category_counts = categorized["CategoryCode"].value_counts().to_dict()
        summary = {
            "category_counts": {str(k): int(v) for k, v in category_counts.items()},
            "pending_classification_count": int((categorized["ClassificationStatus"] == "needs_review").sum()),
            "recurring_candidate_count": len(patterns),
            "approved_recurring_count": sum(p.approved for p in patterns),
        }
        # Raw rows and original descriptions deliberately remain outside ADK state.
        yield _event(self, "Transactions classified and recurring patterns assessed.", {"transaction_analysis": summary})


class CashForecastWorkflowAgent(BaseAgent):
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        csv_path = str(ctx.session.state["csv_path"])
        validation, _, patterns, forecast = compute_forecast(csv_path, review=False)
        summary = {
            "as_of_date": forecast.as_of_date,
            "current_cash": forecast.current_cash,
            "monthly_operating_burn": forecast.monthly_operating_burn,
            "minimum_cash_reserve": forecast.safe_cash_reserve,
            "forecast_confidence": forecast.confidence,
            "scenarios": forecast.summary_dicts(),
            "approved_recurring_count": sum(p.approved for p in patterns),
            "unclassified_count": forecast.unclassified_count,
            "unclassified_outflow": forecast.unclassified_outflow,
            "unclassified_inflow": forecast.unclassified_inflow,
            "source": "verified_python_calculation",
        }
        yield _event(self, "90-day and 12-month cash forecasts calculated.", {"forecast_analysis": summary})


class OfflineCFOBriefingAgent(BaseAgent):
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        forecast = ctx.session.state["forecast_analysis"]
        base = next(s for s in forecast["scenarios"] if s["key"] == "base")
        if base["runway_months"] is not None:
            runway_sentence = f"Base Case runway is {base['runway_months']} months. "
        else:
            runway_sentence = (
                "The Base Case is net cash positive, so no runway limit applies. "
            )
        briefing = (
            f"Verified closing cash is KRW {forecast['current_cash']:,.0f}. "
            + runway_sentence
            + f"Projected cash depletion is {base['exhaustion_date'] or 'not within 12 months'}. "
            + "Review pending recurring items and finalize liquidity actions before the first risk date."
        )
        if forecast.get("unclassified_count"):
            briefing += (
                f" Note: {forecast['unclassified_count']} unclassified transactions "
                f"totaling KRW {forecast['unclassified_outflow'] + forecast['unclassified_inflow']:,.0f} "
                "were conservatively included; run --review to classify them."
            )
        yield _event(self, briefing, {"cfo_briefing": briefing, "briefing_mode": "deterministic_fallback"})


class ReportBuilderAgent(BaseAgent):
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        result = run_pipeline(
            str(ctx.session.state["csv_path"]),
            str(ctx.session.state["output_path"]),
            review=False,
            briefing=str(ctx.session.state.get("cfo_briefing", "")),
        )
        public_result = {k: v for k, v in result.items() if k != "recurring_candidates"}
        yield _event(self, "CFO Cash Control Tower HTML generated.", {"final_result": public_result})


def build_root_agent(use_llm: bool = True) -> SequentialAgent:
    if use_llm:
        cash_skill = load_skill_from_dir(SKILL_DIR)
        tools = skill_toolset.SkillToolset(skills=[cash_skill])
        briefing_agent: BaseAgent = LlmAgent(
            name="CFOBriefingAgent",
            model=MODEL,
            description="Produces an evidence-based CFO cash briefing using the cash runway skill.",
            instruction=(
                "Use the analyze-cash-runway skill. Use only {forecast_analysis} and "
                "{transaction_analysis}. Never create or recalculate amounts. Return concise professional English."
            ),
            tools=[tools],
            output_key="cfo_briefing",
            include_contents="none",
        )
    else:
        briefing_agent = OfflineCFOBriefingAgent(
            name="CFOBriefingAgent",
            description="Produces a deterministic briefing when no Gemini API key is available.",
        )

    return SequentialAgent(
        name="CFOCashControlTower",
        description="ADK workflow: validation, transaction analysis, forecast, CFO briefing, and HTML report.",
        sub_agents=[
            DataValidatorAgent(name="DataValidatorAgent", description="Validates local bank CSV and reconciles balances."),
            TransactionAnalystAgent(name="TransactionAnalystAgent", description="Classifies transactions and assesses recurring candidates."),
            CashForecastWorkflowAgent(name="CashForecastAgent", description="Calculates deterministic 90-day and 12-month forecasts."),
            briefing_agent,
            ReportBuilderAgent(name="ReportBuilderAgent", description="Builds the final English HTML dashboard."),
        ],
    )


root_agent = build_root_agent(use_llm=True)
