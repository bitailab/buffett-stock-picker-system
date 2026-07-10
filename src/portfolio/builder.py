"""一次性建仓组合构建 —— 基于估值回归的预期收益模型。

模型（保守假设，非收益承诺）：
  1. 内在价值按该股票的股东盈余增速 g 成长：fair(h) = intrinsic × (1+g)^h
  2. 价格在约 3 年内向内在价值"部分回归"：回归比例 = min(h/3, 1) × 70%
     （70% 折扣承认市场可能长期不给修复）
  3. 预期期末价 = 现价 + (fair(h) − 现价) × 回归比例
  4. 年化预期收益 = (期末价/现价)^(1/h) − 1（不含股息，偏保守）

只在质量核心标准全部通过的股票中选择（先是好生意，再谈价格），
按年化预期收益排序取前 N 只，权重与预期收益成正比、单只封顶，
凑不满的资金建议持有现金。
"""
from dataclasses import dataclass, field

CONVERGENCE_YEARS = 3.0    # 价格向价值回归所需年数假设
CONVERGENCE_HAIRCUT = 0.7  # 回归折扣（30% 概率不修复）
MAX_WEIGHT = 0.30          # 单只上限
MAX_POSITIONS = 5          # 巴菲特式集中：最多 5 只


@dataclass
class PositionPlan:
    symbol: str
    name: str
    sector: str
    price: float
    intrinsic: float
    total_score: float
    margin_of_safety: float
    growth: float               # 采用的股东盈余增速
    exp_annual_return: float    # 年化预期收益（小数）
    exp_total_return: float     # 持有期累计预期收益（小数）
    weight: float               # 组合权重（小数）
    alloc_usd: float
    whole_shares: int
    whole_shares_usd: float


@dataclass
class LumpSumPlan:
    amount_usd: float
    years: float
    positions: list[PositionPlan] = field(default_factory=list)
    cash_usd: float = 0.0
    cash_reason: str = ""
    exp_annual_return: float = 0.0   # 组合加权年化预期收益
    exp_value_at_horizon: float = 0.0


def expected_return(price: float, intrinsic: float, growth: float,
                    years: float) -> tuple[float, float]:
    """返回 (年化预期收益, 累计预期收益)。"""
    fair_h = intrinsic * (1 + growth) ** years
    convergence = min(years / CONVERGENCE_YEARS, 1.0) * CONVERGENCE_HAIRCUT
    exp_price = price + (fair_h - price) * convergence
    if exp_price <= 0:
        return -1.0, -1.0
    total = exp_price / price - 1
    annual = (1 + total) ** (1 / years) - 1
    return annual, total


def build_lumpsum(scores: list[dict], amount_usd: float, years: float) -> LumpSumPlan:
    """scores 为 scoring.load_scores() 的结果（含 valuation dict）。"""
    years = max(0.25, float(years))
    plan = LumpSumPlan(amount_usd=amount_usd, years=years)

    candidates = []
    for s in scores:
        v = s.get("valuation") or {}
        price, intrinsic = s.get("price"), s.get("intrinsic_value")
        if not (s.get("quality_passed") and price and intrinsic):
            continue
        growth = v.get("growth_used") or 0.0
        annual, total = expected_return(price, intrinsic, growth, years)
        if annual <= 0:
            continue
        candidates.append((annual, total, growth, s))
    candidates.sort(key=lambda x: x[0], reverse=True)
    top = candidates[:MAX_POSITIONS]

    if not top:
        plan.cash_usd = amount_usd
        plan.cash_reason = ("当前没有质量达标且预期收益为正的股票——按模型假设，"
                            "现在全仓买入大概率跑不赢现金/短债。建议持币等待，"
                            "或降低 config.yaml 中的质量/估值门槛后重新计算。")
        return plan

    # 权重 ∝ 年化预期收益，单只封顶后归一化
    raw = [a for a, *_ in top]
    weights = [min(r / sum(raw), MAX_WEIGHT) for r in raw]
    weights = [w / sum(weights) for w in weights]
    # 封顶归一化后仍可能有超限，再截一次并把余量记为现金
    weights = [min(w, MAX_WEIGHT) for w in weights]
    invested_frac = sum(weights)

    exp_annual_weighted = 0.0
    for (annual, total, growth, s), w in zip(top, weights):
        alloc = amount_usd * w
        shares = int(alloc // s["price"])
        plan.positions.append(PositionPlan(
            symbol=s["symbol"], name=s["name"], sector=s["sector"],
            price=s["price"], intrinsic=s["intrinsic_value"],
            total_score=s["total_score"], margin_of_safety=s["margin_of_safety"],
            growth=growth,
            exp_annual_return=annual, exp_total_return=total,
            weight=w, alloc_usd=round(alloc, 2),
            whole_shares=shares, whole_shares_usd=round(shares * s["price"], 2),
        ))
        exp_annual_weighted += annual * (w / invested_frac)

    plan.cash_usd = round(amount_usd * (1 - invested_frac), 2)
    if plan.cash_usd > 0.005 * amount_usd:
        plan.cash_reason = f"单只 {MAX_WEIGHT:.0%} 上限生效，未能配置的资金建议持有现金。"
    else:
        plan.cash_usd = 0.0
    plan.exp_annual_return = exp_annual_weighted
    invested = amount_usd - plan.cash_usd
    plan.exp_value_at_horizon = round(
        invested * (1 + exp_annual_weighted) ** years + plan.cash_usd, 2)
    return plan
