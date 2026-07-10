"""第一层：巴菲特质量筛选 —— "好生意"。

输入 FMP 的年报数据（income / balance / cashflow，按年份倒序），
输出每项标准的通过情况和 0-100 的质量分。
"""
from dataclasses import dataclass, field


@dataclass
class Check:
    name: str          # 指标名（中文，报告直接展示）
    principle: str     # 对应的巴菲特原则
    value: str         # 实际值（格式化后的字符串）
    threshold: str     # 通过标准
    passed: bool
    weight: float      # 在质量分中的权重


@dataclass
class QualityResult:
    checks: list[Check] = field(default_factory=list)
    score: float = 0.0           # 0-100
    passed_all_core: bool = False
    years_of_data: int = 0
    insufficient_data: bool = False


def _cagr(first: float, last: float, years: int) -> float | None:
    if years <= 0 or first is None or last is None or first <= 0 or last <= 0:
        return None
    return ((last / first) ** (1 / years) - 1) * 100


def _smoothed_cagr(series: list[float]) -> float | None:
    """端点取两年平均后再算 CAGR，避免一次性损益把首尾任一年污染。

    序列按时间正序。首尾各取 2 个点求均值，其"重心"分别落在 t=0.5 和 t=n-1.5，
    因此跨度为 n-2 年。数据不足 4 年时退回裸端点。
    """
    n = len(series)
    if n < 2:
        return None
    k = 2 if n >= 4 else 1
    base = sum(series[:k]) / k
    end = sum(series[-k:]) / k
    return _cagr(base, end, n - k)


def _safe_div(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


def evaluate_quality(income: list[dict], balance: list[dict],
                     cashflow: list[dict], cfg: dict) -> QualityResult:
    """cfg 为 config.yaml 中的 quality 段。"""
    res = QualityResult()
    n = min(len(income), len(balance), len(cashflow))
    res.years_of_data = n
    if n < cfg["min_years"]:
        res.insufficient_data = True
        return res

    # FMP 返回按年份倒序（最新在前），统一截取共同年份并转为正序（最旧在前）
    inc = list(reversed(income[:n]))
    bal = list(reversed(balance[:n]))
    cfs = list(reversed(cashflow[:n]))

    # ---------- 逐年 ROE ----------
    roes = []
    for i, b in zip(inc, bal):
        roe = _safe_div(i.get("netIncome"), b.get("totalStockholdersEquity"))
        if roe is not None:
            roes.append(roe * 100)
    roe_avg = sum(roes) / len(roes) if roes else None
    roe_min = min(roes) if roes else None
    res.checks.append(Check(
        name="ROE（净资产收益率）",
        principle="长期高资本回报，好生意的核心特征",
        value=f"平均 {roe_avg:.1f}%，最低 {roe_min:.1f}%" if roe_avg is not None else "数据缺失",
        threshold=f"平均 ≥ {cfg['roe_avg_min']}% 且每年 ≥ {cfg['roe_floor']}%",
        passed=(roe_avg is not None and roe_avg >= cfg["roe_avg_min"]
                and roe_min >= cfg["roe_floor"]),
        weight=2.0,
    ))

    # ---------- 毛利率 ----------
    gms = [_safe_div(i.get("grossProfit"), i.get("revenue")) for i in inc]
    gms = [g * 100 for g in gms if g is not None]
    gm_avg = sum(gms) / len(gms) if gms else None
    res.checks.append(Check(
        name="毛利率",
        principle="定价权是护城河最直接的证据",
        value=f"平均 {gm_avg:.1f}%" if gm_avg is not None else "数据缺失",
        threshold=f"≥ {cfg['gross_margin_min']}%",
        passed=gm_avg is not None and gm_avg >= cfg["gross_margin_min"],
        weight=1.0,
    ))

    # ---------- 净利率 ----------
    nms = [_safe_div(i.get("netIncome"), i.get("revenue")) for i in inc]
    nms = [m * 100 for m in nms if m is not None]
    nm_avg = sum(nms) / len(nms) if nms else None
    res.checks.append(Check(
        name="净利率",
        principle="真正赚钱的生意，而不是空转的营收",
        value=f"平均 {nm_avg:.1f}%" if nm_avg is not None else "数据缺失",
        threshold=f"≥ {cfg['net_margin_min']}%",
        passed=nm_avg is not None and nm_avg >= cfg["net_margin_min"],
        weight=1.0,
    ))

    # ---------- 盈利记录 ----------
    # 「无亏损」看 EPS（股东视角）；「增长率」看营业利润。
    # EPS 会被处置收益、税务返还、汇兑等非经营损益污染：JNJ 分拆 Kenvue 那年
    # 净利润 351 亿远超营业利润 220 亿，ABT 与 Gartner 也各有一个净利润 > 营业利润
    # 的年份。用这类年份做端点，CAGR 会凭空翻正或翻负。营业利润不含这些项目。
    eps_series = [i.get("epsDiluted") or i.get("epsdiluted") or i.get("eps") for i in inc]
    eps_series = [e for e in eps_series if e is not None]
    no_loss = bool(eps_series) and all(e > 0 for e in eps_series)

    op_series = [i.get("operatingIncome") for i in inc]
    op_series = [o for o in op_series if o is not None]
    op_cagr = _smoothed_cagr(op_series) if op_series and all(o > 0 for o in op_series) else None

    min_cagr = cfg.get("earnings_cagr_min", cfg.get("eps_cagr_min", 5))
    res.checks.append(Check(
        name="盈利记录",
        principle="盈利稳定可预测，看得懂未来十年",
        value=(f"{len(eps_series)} 年无亏损，营业利润 CAGR {op_cagr:.1f}%"
               if no_loss and op_cagr is not None
               else ("有亏损年份" if eps_series and not no_loss
                     else ("营业利润为负或缺失" if eps_series else "数据缺失"))),
        threshold=f"无亏损年份且 营业利润 CAGR ≥ {min_cagr}%",
        passed=no_loss and op_cagr is not None and op_cagr >= min_cagr,
        weight=1.5,
    ))

    # ---------- 负债水平（最新一年）----------
    latest_bal, latest_inc = bal[-1], inc[-1]
    total_debt = latest_bal.get("totalDebt")
    equity = latest_bal.get("totalStockholdersEquity")
    ebitda = latest_inc.get("ebitda")
    d2e = _safe_div(total_debt, equity)
    d2ebitda = _safe_div(total_debt, ebitda)
    # 债务/自由现金流：分母取最近三年平均 FCF，抹平单年波动。
    # FCF 已扣除资本开支，是芒格反对 EBITDA 时所指的"真钱"口径。
    fcfs = [c.get("freeCashFlow") for c in cfs[-3:]]
    fcfs = [f for f in fcfs if f is not None]
    avg_fcf = (sum(fcfs) / len(fcfs)) if fcfs else None
    d2fcf = _safe_div(total_debt, avg_fcf) if (avg_fcf or 0) > 0 else None

    rule = cfg.get("debt_rule", "or_ebitda")
    ok_d2e = d2e is not None and 0 <= d2e < cfg["max_debt_to_equity"]
    ok_ebitda = d2ebitda is not None and d2ebitda < cfg["max_debt_to_ebitda"]
    ok_fcf = d2fcf is not None and d2fcf < cfg.get("max_debt_to_fcf", 5)
    debt_free = total_debt is not None and total_debt <= 0

    if debt_free:
        debt_ok = True
    elif rule == "and_fcf":
        # 两项都要过；某项算不出（如保险股无 EBITDA、亏损公司无正 FCF）则只看可得的那项
        available = [v for v, avail in ((ok_d2e, d2e is not None), (ok_fcf, d2fcf is not None)) if avail]
        debt_ok = all(available) if available else False
    elif rule == "fcf_only":
        debt_ok = ok_fcf if d2fcf is not None else ok_d2e
    else:  # or_ebitda：原行为
        debt_ok = ok_ebitda or ok_d2e

    parts = []
    if d2e is not None:
        parts.append(f"D/E {d2e:.2f}")
    if d2ebitda is not None:
        parts.append(f"债务/EBITDA {d2ebitda:.2f}")
    if d2fcf is not None:
        parts.append(f"债务/FCF {d2fcf:.2f}")
    thresholds = {
        "and_fcf": f"D/E < {cfg['max_debt_to_equity']} 且 债务/FCF < {cfg.get('max_debt_to_fcf', 5)}",
        "fcf_only": f"债务/FCF < {cfg.get('max_debt_to_fcf', 5)}",
    }
    res.checks.append(Check(
        name="负债水平",
        principle="不靠杠杆赚钱，风浪来了不翻船",
        value="无有息负债" if debt_free else (
            "，".join(parts) if parts else "数据缺失"),
        threshold=thresholds.get(
            rule,
            f"债务/EBITDA < {cfg['max_debt_to_ebitda']} 或 D/E < {cfg['max_debt_to_equity']}"),
        passed=debt_ok,
        weight=1.5,
    ))

    # ---------- 利息覆盖倍数 ----------
    ebit = latest_inc.get("operatingIncome")
    interest = latest_inc.get("interestExpense")
    if interest is not None and interest <= 0:
        cover_ok, cover_str = True, "无利息支出"
    else:
        cover = _safe_div(ebit, interest)
        cover_ok = cover is not None and cover > cfg["min_interest_coverage"]
        cover_str = f"{cover:.1f} 倍" if cover is not None else "数据缺失"
    res.checks.append(Check(
        name="利息覆盖倍数",
        principle="财务稳健，利润轻松覆盖利息",
        value=cover_str,
        threshold=f"> {cfg['min_interest_coverage']} 倍",
        passed=cover_ok,
        weight=1.0,
    ))

    # ---------- FCF 转化率 ----------
    ratios = []
    for c in cfs:
        r = _safe_div(c.get("freeCashFlow"), c.get("netIncome"))
        if r is not None and c.get("netIncome", 0) > 0:
            ratios.append(r * 100)
    fcf_conv = sum(ratios) / len(ratios) if ratios else None
    res.checks.append(Check(
        name="FCF/净利润",
        principle="利润必须是真金白银的现金流",
        value=f"平均 {fcf_conv:.0f}%" if fcf_conv is not None else "数据缺失",
        threshold=f"≥ {cfg['fcf_conversion_min']}%",
        passed=fcf_conv is not None and fcf_conv >= cfg["fcf_conversion_min"],
        weight=1.5,
    ))

    # ---------- 股本变化（加分项）----------
    shares = [i.get("weightedAverageShsOutDil") or i.get("weightedAverageShsOut") for i in inc]
    shares = [s for s in shares if s]
    buyback = len(shares) >= 2 and shares[-1] < shares[0]
    chg = ((shares[-1] / shares[0] - 1) * 100) if len(shares) >= 2 else None
    res.checks.append(Check(
        name="股本回购（加分项）",
        principle="管理层用回购回报股东",
        value=f"流通股 {chg:+.1f}%" if chg is not None else "数据缺失",
        threshold="流通股数下降",
        passed=buyback,
        weight=0.5,
    ))

    # ---------- 质量分 ----------
    core = [c for c in res.checks if c.name != "股本回购（加分项）"]
    total_w = sum(c.weight for c in core)
    earned = sum(c.weight for c in core if c.passed)
    score = earned / total_w * 100
    if buyback:
        score = min(100.0, score + 5)   # 回购加 5 分
    res.score = round(score, 1)
    res.passed_all_core = all(c.passed for c in core)
    return res
