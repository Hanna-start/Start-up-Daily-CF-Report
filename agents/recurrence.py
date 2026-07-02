"""과거 거래에서 월간 반복 후보를 찾는다. 승인된 후보만 예측에 사용한다."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict

import pandas as pd

from .categorizer import ApprovalStore


@dataclass
class RecurringPattern:
    pattern_id: str
    description: str
    direction: str
    amount: float
    day_of_month: int
    occurrences: int
    interval_days: float
    amount_cv: float
    confidence: float
    approved: bool
    category_code: str

    def as_dict(self) -> dict:
        return asdict(self)


class RecurrenceDetectorAgent:
    def __init__(self, store: ApprovalStore | None = None):
        self.store = store or ApprovalStore()

    def detect(self, df: pd.DataFrame) -> list[RecurringPattern]:
        candidates: list[RecurringPattern] = []
        work = df.copy()
        work["Direction"] = work.apply(lambda r: "inflow" if r["Deposit"] > 0 else "outflow", axis=1)
        work["Amount"] = work[["Deposit", "Withdrawal"]].max(axis=1)
        for (description, direction), group in work.groupby(["Description", "Direction"]):
            group = group.sort_values("Date")
            if len(group) < 3 or group["Date"].dt.to_period("M").nunique() < 3:
                continue
            intervals = group["Date"].diff().dt.days.dropna()
            mean_interval = float(intervals.mean())
            if not 20 <= mean_interval <= 40:
                continue
            mean_amount = float(group["Amount"].median())
            amount_cv = float(group["Amount"].std(ddof=0) / group["Amount"].mean()) if group["Amount"].mean() else 0.0
            day = int(round(group["Date"].dt.day.median()))
            interval_score = max(0.0, 1 - abs(mean_interval - 30.4) / 15)
            confidence = round(min(0.98, 0.45 + min(len(group), 8) * 0.05 + interval_score * 0.2 + max(0, 0.15 - amount_cv)), 2)
            raw_id = f"{description}|{direction}"
            pattern_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:12]
            approval = self.store.data["recurrences"].get(pattern_id, {})
            category = str(group.iloc[-1].get("CategoryCode", "unknown"))
            candidates.append(RecurringPattern(pattern_id, str(description), direction, mean_amount, day, len(group), mean_interval, amount_cv, confidence, bool(approval.get("approved", False)), category))
        return sorted(candidates, key=lambda p: (-p.confidence, p.description))
