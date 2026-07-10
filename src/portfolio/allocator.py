"""每月 5000 SGD 分配算法 —— 巴菲特风格：集中投资、等待好球。

规则：
1. 候选 = 质量核心标准全过 且 安全边际 ≥ 阈值，按综合分排序
2. 单只持仓占组合市值 > max_position_pct 的跳过（避免过度集中）
3. 资金按综合分加权分给前 1-3 名
4. 没有候选 → 建议持币观望（"没有好球就不挥棒"）
"""
from dataclasses import dataclass, field

from .tracker import Holding


@dataclass
class BuySuggestion:
    symbol: str
    name: str
    price: float
    total_score: float
    margin_of_safety: float
    alloc_usd: float          # 分给这只股票的美元金额
    alloc_sgd: float
    whole_shares: int         # 整股方案：股数
    whole_shares_usd: float   # 整股方案：实际花费
    fractional_usd: float     # 碎股方案：直接按金额买


@dataclass
class AllocationPlan:
    budget_sgd: float
    budget_usd: float
    fx_sgd_per_usd: float     # 1 USD = ? SGD
    suggestions: list[BuySuggestion] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)   # 因集中度被跳过的
    hold_cash: bool = False
    hold_cash_reason: str = ""


def build_plan(scores: list[dict], holdings: list[Holding],
               budget_sgd: float, sgd_usd_rate: float, cfg: dict) -> AllocationPlan:
    """scores 为 scoring.load_scores() 的结果；sgd_usd_rate = 1 SGD 兑多少 USD。"""
    budget_usd = budget_sgd * sgd_usd_rate
    plan = AllocationPlan(
        budget_sgd=budget_sgd, budget_usd=round(budget_usd, 2),
        fx_sgd_per_usd=round(1 / sgd_usd_rate, 4),
    )

    weight_by_symbol = {h.symbol: (h.weight_pct or 0) for h in holdings}
    max_pos = cfg["investment"]["max_position_pct"]

    candidates = []
    for s in scores:
        if not s.get("buyable") or not s.get("price"):
            continue
        if weight_by_symbol.get(s["symbol"], 0) > max_pos:
            plan.skipped.append({
                "symbol": s["symbol"],
                "reason": f"已占组合 {weight_by_symbol[s['symbol']]:.0f}%，超过 {max_pos}% 上限",
            })
            continue
        candidates.append(s)

    if not candidates:
        plan.hold_cash = True
        plan.hold_cash_reason = (
            "本月没有股票同时满足质量标准和安全边际要求。"
            "巴菲特：投资就像打棒球，没有好球就不挥棒——建议本月持有现金等待更好的价格。"
        )
        return plan

    top = candidates[: cfg["investment"]["max_buys_per_month"]]
    total_score = sum(c["total_score"] for c in top)
    for c in top:
        alloc_usd = budget_usd * c["total_score"] / total_score
        shares = int(alloc_usd // c["price"])
        plan.suggestions.append(BuySuggestion(
            symbol=c["symbol"], name=c["name"], price=c["price"],
            total_score=c["total_score"],
            margin_of_safety=c["margin_of_safety"],
            alloc_usd=round(alloc_usd, 2),
            alloc_sgd=round(alloc_usd / sgd_usd_rate, 2),
            whole_shares=shares,
            whole_shares_usd=round(shares * c["price"], 2),
            fractional_usd=round(alloc_usd, 2),
        ))
    return plan
