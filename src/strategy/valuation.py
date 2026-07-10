"""第二层：估值 —— "好价格"。

Owner Earnings（股东盈余）DCF：
  股东盈余 = 净利润 + 折旧摊销 − 维持性资本开支
  维持性资本开支近似取 min(|资本开支|, 折旧摊销)—— 超出折旧的部分视为增长性开支。
两阶段模型：前 5 年用历史增长率（上限 10%），后 5 年增速减半，之后按终值增长率折现。
"""
from dataclasses import dataclass


@dataclass
class ValuationResult:
    intrinsic_per_share: float | None = None
    price: float | None = None
    margin_of_safety: float | None = None   # %，正数 = 低估
    growth_used: float | None = None        # 采用的第一阶段增长率
    owner_earnings: float | None = None     # 最近三年平均股东盈余（总额）
    earnings_yield: float | None = None     # 盈利收益率 %
    beats_treasury: bool | None = None      # 盈利收益率是否高于 10 年期国债
    note: str = ""


def _owner_earnings(inc: dict, cf: dict) -> float | None:
    ni = inc.get("netIncome")
    da = cf.get("depreciationAndAmortization") or 0
    capex = abs(cf.get("capitalExpenditure") or 0)
    if ni is None:
        return None
    maintenance_capex = min(capex, da)
    formula = ni + da - maintenance_capex
    # 保守修正：与 FCF 取较小者。若 D&A 来自资本化内容/无形资产的摊销
    # （如流媒体的内容库），对应支出走经营现金流而非 capex，
    # 上面的公式会虚增股东盈余；FCF 已如实扣除这类支出。
    fcf = cf.get("freeCashFlow")
    return min(formula, fcf) if fcf is not None else formula


def evaluate_valuation(income: list[dict], cashflow: list[dict],
                       price: float | None, shares_outstanding: float | None,
                       cfg: dict, treasury_10y: float | None = None) -> ValuationResult:
    """income/cashflow 按 FMP 原始顺序（最新在前）。cfg 为 config.yaml 的 valuation 段。"""
    res = ValuationResult(price=price)
    n = min(len(income), len(cashflow))
    if n < 3 or not price or not shares_outstanding:
        res.note = "数据不足，无法估值"
        return res

    inc = list(reversed(income[:n]))
    cfs = list(reversed(cashflow[:n]))

    # 最近三年平均股东盈余，抹平单年波动
    recent = [_owner_earnings(i, c) for i, c in zip(inc[-3:], cfs[-3:])]
    recent = [o for o in recent if o is not None]
    if not recent:
        res.note = "无法计算股东盈余"
        return res
    oe = sum(recent) / len(recent)
    res.owner_earnings = oe
    if oe <= 0:
        res.note = "股东盈余为负，不适用 DCF"
        return res

    # 历史股东盈余 CAGR 作为增长率，保守封顶
    oldest = [_owner_earnings(i, c) for i, c in zip(inc[:3], cfs[:3])]
    oldest = [o for o in oldest if o is not None and o > 0]
    growth = 0.0
    if oldest and n >= 4:
        base = sum(oldest) / len(oldest)
        span = n - 3
        if base > 0 and span > 0:
            growth = (oe / base) ** (1 / span) - 1
    growth = max(0.0, min(growth, cfg["max_growth_rate"]))
    res.growth_used = growth

    # 两阶段 DCF
    r = cfg["discount_rate"]
    g_term = cfg["terminal_growth"]
    years = cfg["projection_years"]
    stage1 = years // 2

    pv, cash = 0.0, oe
    for year in range(1, years + 1):
        g = growth if year <= stage1 else growth / 2
        cash *= (1 + g)
        pv += cash / (1 + r) ** year
    terminal = cash * (1 + g_term) / (r - g_term)
    pv += terminal / (1 + r) ** years

    intrinsic = pv / shares_outstanding
    res.intrinsic_per_share = intrinsic
    res.margin_of_safety = (1 - price / intrinsic) * 100

    # 辅助校验：盈利收益率 vs 10 年期国债
    latest_eps = inc[-1].get("epsDiluted") or inc[-1].get("epsdiluted") or inc[-1].get("eps")
    if latest_eps and price:
        res.earnings_yield = latest_eps / price * 100
        if treasury_10y is not None:
            res.beats_treasury = res.earnings_yield > treasury_10y
    return res
