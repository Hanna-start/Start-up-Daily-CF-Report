"""일일 자금일보를 기본으로 하는 단일 HTML Control Tower."""

from __future__ import annotations

import html
import json
from pathlib import Path

import pandas as pd

from .data_normalizer import NormalizationResult
from .forecaster import ForecastResult


def money(value: float) -> str:
    return f"(KRW {abs(value):,.0f})" if value < 0 else f"KRW {value:,.0f}"


class CFOAnalystAgent:
    def build_actions(self, forecast: ForecastResult) -> list[str]:
        base = next(s for s in forecast.summaries if s.key == "base")
        actions: list[str] = []
        if forecast.current_cash < forecast.safe_cash_reserve:
            actions.append("Tighten today's payment approvals by separating critical spend from deferrable spend.")
        if base.exhaustion_date:
            actions.append(f"Finalize additional funding or cost reductions before the projected cash depletion date of {base.exhaustion_date}.")
        actions.append("Review recurring items pending approval to improve the next forecast cycle.")
        return actions[:3]

    @staticmethod
    def _daily_snapshot(validation: NormalizationResult) -> dict:
        df = validation.data.sort_values(["Date", "SourceRow"], kind="stable")
        as_of = df["Date"].max()
        today = df[df["Date"].dt.normalize() == as_of.normalize()]
        prior = df[df["Date"].dt.normalize() < as_of.normalize()]
        prior_cash = float(prior.iloc[-1]["Balance"]) if not prior.empty and pd.notna(prior.iloc[-1]["Balance"]) else None
        current = float(validation.current_cash or 0)
        change = current - prior_cash if prior_cash is not None else float(today["Deposit"].sum() - today["Withdrawal"].sum())
        return {
            "inflow": float(today["Deposit"].sum()),
            "outflow": float(today["Withdrawal"].sum()),
            "change": change,
            "prior_cash": prior_cash,
            "transactions": today.sort_values(["Withdrawal", "Deposit"], ascending=False).head(5),
        }

    def analyze_and_report(self, forecast: ForecastResult, validation: NormalizationResult, patterns: list, output_path: str | Path = "output/cfo_control_tower.html", briefing: str | None = None) -> str:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        snap = self._daily_snapshot(validation)
        summaries = forecast.summary_dicts()
        base = next(x for x in summaries if x["key"] == "base")
        reserve_ratio = min(100, max(0, forecast.current_cash / forecast.safe_cash_reserve * 100)) if forecast.safe_cash_reserve else 100
        change_class = "down" if snap["change"] < 0 else "up"
        change_arrow = "↓" if snap["change"] < 0 else "↑"
        demo_names = {
            "가상 기초잔액": "Opening Balance",
            "가상 고객사 정산": "Northstar Customer Settlement",
            "가상 사무실 임차료": "Harbor Office Rent",
            "가상 클라우드 이용료": "Atlas Cloud Services",
            "가상 부품 공급사": "Pioneer Components",
            "가상 임직원 급여": "Payroll",
            "별빛은행 운영계좌": "Starlight Bank - Operating Account",
        }
        actions = "".join(f'<li><span>{i}</span>{html.escape(x)}</li>' for i, x in enumerate(self.build_actions(forecast), 1))
        tx_rows = "".join(
            f'<tr><td>{html.escape(demo_names.get(str(row.Description), str(row.Description)))}</td><td class="num {"down" if row.Withdrawal else "up"}">{money(row.Deposit - row.Withdrawal)}</td></tr>'
            for row in snap["transactions"].itertuples()
        ) or '<tr><td colspan="2">No transactions today</td></tr>'
        latest_accounts = (
            validation.data.dropna(subset=["Balance"])
            .sort_values(["Date", "SourceRow"], kind="stable")
            .groupby("Account", as_index=False)
            .tail(1)
            .sort_values("Balance", ascending=False)
        )
        account_rows = "".join(
            f'<tr><td><b>{html.escape(demo_names.get(str(row.Account), str(row.Account)))}</b><small class="account-date">As of {row.Date.strftime("%Y-%m-%d")}</small></td><td class="num">{money(float(row.Balance))}</td></tr>'
            for row in latest_accounts.itertuples()
        ) or '<tr><td colspan="2">No account balance data available</td></tr>'
        account_total = float(latest_accounts["Balance"].sum()) if validation.balance_reliable else float(validation.current_cash or 0)
        account_status = "Reconciled" if validation.balance_reliable else "Review required"
        scenario_cards = "".join(
            f'<article class="scenario {s["key"]}"><span>{html.escape(s["label"])}</span><b>{money(s["ending_cash_90d"])}</b><small>90-day closing cash · Runway: {s["runway_months"] if s["runway_months"] is not None else "Net cash inflow"}{" months" if s["runway_months"] is not None else ""}</small></article>'
            for s in summaries
        )
        pattern_waiting = sum(not p.approved for p in patterns)
        issue_messages = {
            "INVALID_DATE": "Transaction date could not be parsed.", "INVALID_AMOUNT": "Transaction amount could not be parsed.",
            "NEGATIVE_AMOUNT": "A transaction amount is negative.", "BOTH_DIRECTIONS": "A transaction contains both an inflow and an outflow.",
            "ZERO_TRANSACTION": "A transaction has zero inflow and zero outflow.", "EMPTY_DESCRIPTION": "Transaction description is blank.",
            "POSSIBLE_DUPLICATE": "Potential duplicate transaction detected.", "BALANCE_MISMATCH": "Reported balance does not reconcile to transaction activity.",
            "UNRELIABLE_BALANCE": "Account balance continuity is unreliable; the latest reported balance is used provisionally.",
            "NO_BALANCE": "No balance column is available; current cash is based on net transaction activity.",
        }
        issues = "".join(f'<li>{issue_messages.get(i.code, html.escape(i.code))}{f" · CSV row {i.row}" if i.row else ""}</li>' for i in validation.issues) or "<li>Bank balances reconcile to transaction activity.</li>"
        assumptions = "".join(f"<li>{html.escape(x)}</li>" for x in forecast.assumptions)
        briefing_html = html.escape(briefing or "Briefing generated from verified Python calculations. Add a Gemini API key to enable an AI-written CFO narrative.").replace("\n", "<br>")
        daily_json = json.dumps([{"date": r.Date.strftime("%m-%d"), "scenario": r.Scenario, "cash": round(r.EndingCash)} for r in forecast.daily.itertuples()], ensure_ascii=False)
        monthly_json = json.dumps([
            {"month": r.Month, "scenario": r.Scenario, "inflow": round(r.Inflow), "outflow": round(r.Outflow), "cash": round(r.EndingCash)}
            for r in forecast.monthly.itertuples()
        ], ensure_ascii=False)

        content = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CFO Cash Control Tower</title>
<style>
:root{{--ink:#10233f;--muted:#728096;--line:#e7ebf1;--bg:#f4f6f9;--blue:#2864dc;--mint:#10a47b;--red:#db4b4b;--amber:#f2a93b}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);font-family:Inter,"Malgun Gothic",Arial,sans-serif;color:var(--ink)}}header{{background:#fff;border-bottom:1px solid var(--line);padding:18px 4vw;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:3}}header h1{{font-size:18px;margin:0}}header p{{margin:4px 0 0;color:var(--muted);font-size:12px}}nav{{display:flex;background:#edf1f7;padding:4px;border-radius:10px}}nav button{{border:0;background:transparent;padding:9px 16px;border-radius:7px;color:var(--muted);font-weight:700;cursor:pointer}}nav button.active{{background:white;color:var(--ink);box-shadow:0 2px 7px #182b4a18}}main{{max-width:1180px;margin:auto;padding:26px}}.page{{display:none}}.page.active{{display:block}}.hero{{display:grid;grid-template-columns:1.6fr 1fr 1fr;gap:14px}}.card,.panel,.scenario{{background:#fff;border:1px solid var(--line);border-radius:15px;padding:20px}}.cash{{background:linear-gradient(135deg,#142b4c,#1e4c84);color:#fff;border:0}}.eyebrow{{font-size:12px;color:var(--muted);font-weight:700}}.cash .eyebrow{{color:#b8cae3}}.big{{display:block;font-size:34px;margin:12px 0 8px;letter-spacing:-1px}}.delta{{font-weight:800}}.up{{color:var(--mint)}}.down{{color:var(--red)}}.cash .down{{color:#ffaaa5}}.metric b{{font-size:24px;display:block;margin:13px 0}}.metric small,.scenario small{{color:var(--muted)}}.grid{{display:grid;grid-template-columns:1.5fr 1fr;gap:14px;margin-top:14px}}.panel h2{{font-size:15px;margin:0 0 18px}}canvas{{width:100%;height:230px}}.reserve-head{{display:flex;justify-content:space-between;align-items:end}}.reserve-head b{{font-size:22px}}.track{{height:14px;background:#edf0f4;border-radius:99px;overflow:hidden;margin:18px 0 8px}}.track i{{display:block;height:100%;width:{reserve_ratio:.1f}%;background:linear-gradient(90deg,var(--red),var(--amber));border-radius:99px}}.timeline{{border-left:3px solid #f1c36f;margin:22px 0 0 7px;padding-left:18px}}.timeline b{{display:block;color:var(--red);font-size:18px}}ol{{list-style:none;padding:0;margin:0}}ol li{{display:flex;gap:12px;padding:12px 0;border-bottom:1px solid var(--line);line-height:1.45}}ol li:last-child{{border:0}}ol li span{{flex:0 0 26px;height:26px;border-radius:50%;background:#eaf0ff;color:var(--blue);display:grid;place-items:center;font-weight:800}}table{{width:100%;border-collapse:collapse;font-size:13px}}th{{color:var(--muted);font-weight:600;text-align:left}}th,td{{padding:11px 9px;border-bottom:1px solid var(--line)}}.num{{font-weight:750}}.section-head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}}.section-head h2{{margin:0}}.badge{{font-size:12px;padding:6px 10px;background:#fff4df;color:#8c5a00;border-radius:20px}}.scenarios{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:14px}}.scenario b{{display:block;font-size:22px;margin:10px 0}}.scenario.conservative{{border-top:4px solid var(--red)}}.scenario.base{{border-top:4px solid var(--blue)}}.scenario.optimistic{{border-top:4px solid var(--mint)}}.pill{{padding:4px 8px;border-radius:20px;font-size:11px;background:#edf2ff}}details{{margin-top:12px;background:#fff;border:1px solid var(--line);padding:14px 18px;border-radius:12px}}summary{{font-weight:700;cursor:pointer}}details ul{{color:var(--muted);line-height:1.7}}.footnote{{color:var(--muted);font-size:11px;text-align:center;margin:22px}}@media(max-width:760px){{header{{align-items:flex-start;gap:12px}}main{{padding:14px}}.hero,.grid,.scenarios{{grid-template-columns:1fr}}.big{{font-size:28px}}nav button{{padding:8px 10px}}}}
.account-date{{display:block;color:var(--muted);font-weight:400;margin-top:3px}}.total-row td{{border-top:2px solid var(--ink);border-bottom:0;font-size:15px}}.badge.ok{{background:#e8f7f2;color:#08775a}}.chart-tools{{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:12px;flex-wrap:wrap}}.scenario-switch{{display:flex;background:#edf1f7;padding:3px;border-radius:9px}}.scenario-switch button{{border:0;background:transparent;color:var(--muted);padding:7px 13px;border-radius:7px;font-weight:700;cursor:pointer}}.scenario-switch button.active{{background:#fff;color:var(--ink);box-shadow:0 1px 5px #14233d18}}.legend{{display:flex;gap:16px;color:var(--muted);font-size:12px}}.legend i{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px}}.legend .in{{background:#10a47b}}.legend .out{{background:#ef8b82}}.legend .cashline{{background:#2864dc;height:3px;border-radius:0;vertical-align:3px}}.chart-note{{color:var(--muted);font-size:12px;margin:8px 0 0}}
</style></head><body>
<header><div><h1>CFO Cash Control Tower</h1><p>As of {forecast.as_of_date} · Forecast confidence: {forecast.confidence}</p></div><nav><button class="active" data-page="daily">Daily Cash Position</button><button data-page="monthly">12-Month Outlook</button></nav></header><main>
<section id="daily" class="page active">
<div class="hero"><article class="card cash"><span class="eyebrow">Closing Cash Balance</span><b class="big">{money(forecast.current_cash)}</b><span class="delta {change_class}">{change_arrow} {money(abs(snap['change']))} vs. prior close</span></article><article class="card metric"><span class="eyebrow">Cash Inflows Today</span><b class="up">+{money(snap['inflow'])}</b><small>Actual bank activity</small></article><article class="card metric"><span class="eyebrow">Cash Outflows Today</span><b class="down">-{money(snap['outflow'])}</b><small>Actual bank activity</small></article></div>
<div class="grid"><article class="panel"><div class="section-head"><h2>90-Day Cash Forecast</h2><span class="badge">Base Case</span></div><canvas id="dailyChart"></canvas></article><article class="panel"><h2>Minimum Cash Reserve</h2><div class="reserve-head"><div><span class="eyebrow">Current / Target</span><b>{reserve_ratio:.0f}%</b></div><small>{money(forecast.safe_cash_reserve)}</small></div><div class="track"><i></i></div><small>Six months of normalized operating expenses</small><div class="timeline"><span class="eyebrow">Projected Cash Depletion · Base Case</span><b>{base['exhaustion_date'] or 'Not within 12 months'}</b></div></article></div>
<div class="grid"><article class="panel"><div class="section-head"><h2>Account Balances</h2><span class="badge {'ok' if validation.balance_reliable else ''}">{account_status}</span></div><table><tbody>{account_rows}<tr class="total-row"><td><b>Total Cash</b></td><td class="num"><b>{money(account_total)}</b></td></tr></tbody></table></article><article class="panel"><div class="section-head"><h2>Today's Transactions</h2><span class="badge">{len(snap['transactions'])} items</span></div><table><tbody>{tx_rows}</tbody></table></article></div>
<article class="panel" style="margin-top:14px"><h2>CFO Priorities</h2><ol>{actions}</ol><details><summary>Agent Briefing</summary><p>{briefing_html}</p></details></article>
</section>
<section id="monthly" class="page"><div class="section-head"><div><h2>12-Month Cash Outlook</h2><p class="eyebrow">Management review</p></div><span class="badge">{pattern_waiting} recurring items pending approval</span></div><div class="scenarios">{scenario_cards}</div><article class="panel"><div class="chart-tools"><div><h2>Monthly Cash Flows and Closing Cash</h2><div class="legend"><span><i class="in"></i>Projected Inflows</span><span><i class="out"></i>Projected Outflows</span><span><i class="cashline"></i>Closing Cash</span></div></div><div class="scenario-switch"><button data-scenario="conservative">Downside</button><button class="active" data-scenario="base">Base</button><button data-scenario="optimistic">Upside</button></div></div><canvas id="monthlyChart"></canvas><p class="chart-note">Bars show monthly inflows and outflows. The line shows projected month-end cash; red points indicate a negative cash balance.</p></article><details><summary>Data Validation and Key Assumptions</summary><h3>Validation</h3><ul>{issues}</ul><h3>Assumptions</h3><ul>{assumptions}</ul><p><b>Not available from bank data:</b> contracts, tax schedules, debt maturities, and new sales commitments.</p></details></section>
<p class="footnote">Decision-support analysis based on bank transactions. Not a set of financial statements or investment advice.</p></main>
<script>
const daily={daily_json},monthly={monthly_json},colors={{conservative:'#db4b4b',base:'#2864dc',optimistic:'#10a47b'}};
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{{document.querySelectorAll('nav button,.page').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.page).classList.add('active');if(b.dataset.page==='monthly')drawMonthly(activeScenario)}});
function draw(id,rows){{const c=document.getElementById(id),ctx=c.getContext('2d'),w=c.clientWidth,h=230,d=devicePixelRatio||1;c.width=w*d;c.height=h*d;ctx.scale(d,d);const vals=rows.map(x=>x.cash),mn=Math.min(...vals,0),mx=Math.max(...vals),span=Math.max(1,mx-mn);ctx.clearRect(0,0,w,h);ctx.strokeStyle='#e7ebf1';ctx.beginPath();const zero=12+(mx-0)*(h-30)/span;ctx.moveTo(8,zero);ctx.lineTo(w-8,zero);ctx.stroke();Object.keys(colors).forEach(k=>{{const a=rows.filter(x=>x.scenario===k);ctx.beginPath();ctx.strokeStyle=colors[k];ctx.lineWidth=k==='base'?3:1.5;a.forEach((x,i)=>{{const px=10+i*(w-20)/Math.max(1,a.length-1),py=10+(mx-x.cash)*(h-30)/span;i?ctx.lineTo(px,py):ctx.moveTo(px,py)}});ctx.stroke()}})}}
let activeScenario='base';
function drawMonthly(scenario){{activeScenario=scenario;const rows=monthly.filter(x=>x.scenario===scenario),c=document.getElementById('monthlyChart'),ctx=c.getContext('2d'),w=c.clientWidth,h=280,d=devicePixelRatio||1;c.width=w*d;c.height=h*d;ctx.scale(d,d);const values=rows.flatMap(x=>[x.inflow,x.outflow,x.cash]),mn=Math.min(...values,0),mx=Math.max(...values,0),span=Math.max(1,mx-mn),left=48,right=12,top=14,bottom=34,plotW=w-left-right,plotH=h-top-bottom,y=v=>top+(mx-v)*plotH/span;ctx.clearRect(0,0,w,h);ctx.font='11px sans-serif';ctx.fillStyle='#728096';ctx.textAlign='right';[0,.5,1].forEach(t=>{{const value=mn+(mx-mn)*t,py=y(value);ctx.strokeStyle='#e7ebf1';ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(w-right,py);ctx.stroke();ctx.fillText(Math.round(value/1000000)+'M',left-7,py+4)}});const group=plotW/rows.length,bar=Math.min(15,group*.23);rows.forEach((r,i)=>{{const cx=left+group*(i+.5),zero=y(0);ctx.fillStyle='#10a47b';ctx.fillRect(cx-bar-2,Math.min(y(r.inflow),zero),bar,Math.abs(zero-y(r.inflow)));ctx.fillStyle='#ef8b82';ctx.fillRect(cx+2,Math.min(y(r.outflow),zero),bar,Math.abs(zero-y(r.outflow)));ctx.fillStyle='#728096';ctx.textAlign='center';ctx.fillText(r.month.slice(5),cx,h-12)}});ctx.beginPath();ctx.strokeStyle='#2864dc';ctx.lineWidth=3;rows.forEach((r,i)=>{{const x=left+group*(i+.5),py=y(r.cash);i?ctx.lineTo(x,py):ctx.moveTo(x,py)}});ctx.stroke();rows.forEach((r,i)=>{{const x=left+group*(i+.5),py=y(r.cash);ctx.beginPath();ctx.fillStyle=r.cash<0?'#db4b4b':'#2864dc';ctx.arc(x,py,3.5,0,Math.PI*2);ctx.fill()}})}}
document.querySelectorAll('.scenario-switch button').forEach(b=>b.onclick=()=>{{document.querySelectorAll('.scenario-switch button').forEach(x=>x.classList.remove('active'));b.classList.add('active');drawMonthly(b.dataset.scenario)}});
draw('dailyChart',daily);addEventListener('resize',()=>{{draw('dailyChart',daily);if(document.getElementById('monthly').classList.contains('active'))drawMonthly(activeScenario)}});
</script></body></html>'''
        path.write_text(content, encoding="utf-8")
        return str(path)
