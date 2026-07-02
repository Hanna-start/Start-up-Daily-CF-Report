import asyncio
from pathlib import Path

from main import run_adk_pipeline


ROOT = Path(__file__).parents[1]


def test_offline_adk_pipeline_generates_html(monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    output = ROOT / "output" / "test_adk_report.html"
    result = asyncio.run(run_adk_pipeline("sample_bank_data.csv", str(output)))
    assert result["briefing_mode"] == "deterministic_fallback"
    assert result["agent_skill"] == "analyze-cash-runway"
    assert len(result["adk_agents"]) == 5
    assert output.exists()
    html = output.read_text(encoding="utf-8")
    assert "Agent Briefing" in html
    assert "Closing Cash Balance" in html
    output.unlink(missing_ok=True)
