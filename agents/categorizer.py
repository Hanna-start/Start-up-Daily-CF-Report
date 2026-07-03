"""설명 가능한 거래 분류와 사람 승인 저장소."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd


CATEGORIES = {
    "opening_balance": "기초잔액/이월 (예측 제외)",
    "operating_inflow": "영업 유입",
    "operating_variable": "영업 변동비",
    "operating_fixed": "영업 고정비",
    "investing": "투자 활동",
    "financing": "재무 활동",
    "transfer": "계좌대체/제외",
    "unknown": "미분류",
}

# 순서가 곧 우선순위다: 구체적인 규칙(기초잔액/이체/투자/재무)을 포괄적인 영업 규칙보다
# 먼저 검사한다. '대출 입금'처럼 재무 키워드와 '입금'이 함께 있는 적요가 매출로
# 오분류되어 런웨이를 오염시키는 것을 막기 위함이다. '입금' 단독은 자동 확정하지 않고
# 입금 폴백(낮은 확신도 → 사람 검토)으로 내려보낸다.
RULES = [
    (r"기초잔액|기초 잔액|이월잔액|opening balance|balance forward|carry ?over", "opening_balance"),
    (r"이월|계좌대체|자금이체", "transfer"),
    (r"장비|설비|보증금|투자자산", "investing"),
    (r"대출|차입|이자|원금|투자금", "financing"),
    (r"급여|임차|월세|보험|서버|클라우드|aws|아마존웹서비스", "operating_fixed"),
    (r"외주|부품|광고|카드|식대|배달|스타벅스|맥도날드|카카오t", "operating_variable"),
    (r"매출|정산|수익", "operating_inflow"),
]


class ApprovalStore:
    def __init__(self, path: str | Path = "data/approvals.json"):
        self.path = Path(path)
        self.data = {"version": 1, "classifications": {}, "recurrences": {}}
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.update(loaded)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def classification(self, description: str) -> dict | None:
        return self.data["classifications"].get(description)

    def set_classification(self, description: str, category: str, approved: bool = True) -> None:
        if category not in CATEGORIES:
            raise ValueError(f"알 수 없는 카테고리: {category}")
        self.data["classifications"][description] = {"category": category, "approved": approved}
        self.save()

    def set_recurrence(self, pattern_id: str, approved: bool) -> None:
        self.data["recurrences"][pattern_id] = {"approved": bool(approved)}
        self.save()


class CategorizerAgent:
    def __init__(self, approval_store: ApprovalStore | None = None):
        self.store = approval_store or ApprovalStore()

    @staticmethod
    def suggest(description: str, deposit: float, withdrawal: float) -> tuple[str, float, str]:
        text = description.lower().strip()
        for pattern, category in RULES:
            if re.search(pattern, text):
                return category, 0.85, f"설명에서 규칙 '{pattern}'과 일치"
        if deposit > 0:
            return "operating_inflow", 0.55, "입금 거래라는 제한된 단서"
        return "unknown", 0.25, "분류할 단서 부족"

    def categorize(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        if result.empty:
            for col in (
                "CategoryCode",
                "Category",
                "ClassificationConfidence",
                "ClassificationStatus",
                "ClassificationReason",
            ):
                result[col] = pd.Series(dtype=object)
            return result
        rows = []
        first_row_idx: set = set()
        if {"Account", "Balance", "Date"}.issubset(result.columns):
            sort_cols = ["Date", "SourceRow"] if "SourceRow" in result.columns else ["Date"]
            first_row_idx = set(
                result.sort_values(sort_cols, kind="stable")
                .groupby("Account")
                .head(1)
                .index
            )
        for idx, row in result.iterrows():
            description = str(row["Description"])
            saved = self.store.classification(description)
            if saved and saved.get("approved"):
                category, confidence, reason, status = saved["category"], 1.0, "사용자 승인 기록", "approved"
            else:
                if (
                    idx in first_row_idx
                    and float(row["Deposit"]) > 0
                    and float(row["Withdrawal"]) == 0
                    and pd.notna(row.get("Balance"))
                    and abs(float(row["Deposit"]) - float(row["Balance"])) <= 1.0
                ):
                    category, confidence, reason = (
                        "opening_balance",
                        0.9,
                        "계좌 첫 거래의 입금액과 잔액 일치(기초잔액 추정)",
                    )
                else:
                    category, confidence, reason = self.suggest(
                        description,
                        float(row["Deposit"]),
                        float(row["Withdrawal"]),
                    )
                status = "suggested" if confidence >= 0.75 else "needs_review"
            rows.append((category, CATEGORIES[category], confidence, status, reason))
        result[["CategoryCode", "Category", "ClassificationConfidence", "ClassificationStatus", "ClassificationReason"]] = pd.DataFrame(rows, index=result.index)
        return result
