"""ADK-first command-line entry point for CFO Cash Control Tower."""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agents.pipeline import prepare_analysis, review_items, run_pipeline
from agents.security import validate_local_csv, validate_output_path
from cfo_adk.agent import build_root_agent


APP_NAME = "cfo_cash_control_tower"
USER_ID = "local_cfo"


async def run_adk_pipeline(csv_path: str, output_path: str, review: bool = False) -> dict:
    safe_csv = validate_local_csv(csv_path)
    safe_output = validate_output_path(output_path)

    if review:
        _, store, categorized, patterns = prepare_analysis(str(safe_csv))
        review_items(categorized, patterns, store)

    has_api_key = bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))
    root_agent = build_root_agent(use_llm=has_api_key)
    session_service = InMemorySessionService()
    session_id = uuid.uuid4().hex
    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=session_id,
        state={
            "csv_path": str(safe_csv),
            "output_path": str(safe_output),
            "security:raw_rows_shared_with_llm": False,
        },
    )
    runner = Runner(
        app_name=APP_NAME,
        agent=root_agent,
        session_service=session_service,
    )
    message = types.Content(
        role="user",
        parts=[types.Part(text="Analyze the approved local bank CSV and generate the CFO cash report.")],
    )
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=message,
    ):
        if event.author:
            print(f"[ADK] {event.author}")

    final_session = await session_service.get_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=session_id,
    )
    if final_session is None or "final_result" not in final_session.state:
        raise RuntimeError("ADK workflow completed without a final report result.")
    result = dict(final_session.state["final_result"])
    result["adk_agents"] = [agent.name for agent in root_agent.sub_agents]
    result["briefing_mode"] = final_session.state.get("briefing_mode", "gemini_skill")
    result["agent_skill"] = "analyze-cash-runway"
    return result


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Generate an ADK-orchestrated cash runway report from a local bank CSV.")
    parser.add_argument("csv", nargs="?", default="sample_bank_data.csv", help="Bank transaction CSV inside this workspace")
    parser.add_argument("--output", default="output/cfo_control_tower.html", help="HTML report path inside this workspace")
    parser.add_argument("--review", action="store_true", help="Review uncertain classifications and recurring items first")
    parser.add_argument("--open", action="store_true", help="Open the generated HTML in the default browser")
    args = parser.parse_args()
    try:
        result = asyncio.run(run_adk_pipeline(args.csv, args.output, review=args.review))
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}")
        return 1

    print("CFO Cash Control Tower generated through Google ADK")
    print(f"- As of: {result['as_of_date']}")
    print(f"- Closing cash: KRW {result['current_cash']:,.0f}")
    print(f"- Briefing mode: {result['briefing_mode']}")
    print(f"- Agent Skill: {result['agent_skill']}")
    print(f"- HTML: {result['report']}")
    if args.open:
        webbrowser.open(Path(result["report"]).as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["run_adk_pipeline", "run_pipeline"]
