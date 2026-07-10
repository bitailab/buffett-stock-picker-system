"""SEC EDGAR XBRL 数据客户端 —— 接口与 YFClient/FMPClient 一致。

免费、官方、无 key、无每日配额（仅要求 User-Agent 与 ≤10 req/s），
年报深度 10 年以上。行情/汇率/国债/成分股 EDGAR 不提供，继承 YFClient 的实现。

四个必须处理的坑（原型阶段踩出来的，改动前先读）：
  1. 标签必须「逐年回退」，不能选中一个就用到底。ASC 606 准则（2018）前后营收
     标签不同；Alphabet 2025 财年又换回了 `Revenues`。
  2. EDGAR 的 EPS / 股本是「按该次申报时点的口径」，不是统一口径。公司在后续年报里
     只把最近 2-3 个可比年度按拆股追溯调整，更早的年份永远停在拆股前口径。
     例：GOOGL 的 FY2021 EPS 在 2022 年报里是 112.20（拆股前），2023 年后的年报里
     被改成 5.61（拆股后）；而 FY2019 再未出现在后续年报里，至今仍是 49.16。
     **复权因子必须按该事实的「申报日期」算，只累乘申报日之后发生的拆股。**
     若按财年末算，已被追溯调整的年份会复权两次（曾让 ACGL 出现 -66.5% 的假回购）。
  3. 有些公司压根不报某科目：Alphabet/FactSet 无 `GrossProfit`（用营收−成本推导）；
     Apple 近两年不再单列 `InterestExpense`（并入其他收支净额）。
  4. 总债务不能只累加 `LongTermDebtNoncurrent + LongTermDebtCurrent`：Adobe 现代年报
     只报聚合的 `LongTermDebt`（含当期到期部分），漏掉会算出「无有息负债」。
     且 `DebtCurrent` 已含长债当期部分，与 `LongTermDebtCurrent` 相加会重复计算。
"""
import datetime as dt
import json
import time
import urllib.request

from .cache import Cache
from .fmp_client import FMPError
from .yf_client import YFClient

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# SEC 官方 ticker→CIK 映射在公司做控股架构重组后，会指向只有报备费用数据（ffd）
# 的新注册主体，历史财报仍挂在旧 CIK 下。这里手工覆盖。
# 症状：companyfacts 返回 200，但 facts 里只有 'ffd'、没有 'us-gaap'。
TICKER_CIK_OVERRIDES = {
    "XOM": 34088,        # SEC 映射指向 2115436（新控股主体，无 us-gaap）
}
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# ---------- 标签映射：按优先级逐年回退 ----------
DURATION_TAGS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "Revenues", "SalesRevenueNet", "SalesRevenueServicesNet",
                "RevenuesNetOfInterestExpense"],
    "costOfRevenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfServices",
                      "CostOfGoodsSold",
                      "CostOfServicesExcludingDepreciationDepletionAndAmortization"],
    "grossProfit": ["GrossProfit"],
    "operatingIncome": ["OperatingIncomeLoss"],
    "netIncome": ["NetIncomeLoss", "ProfitLoss"],
    "interestExpense": ["InterestExpense", "InterestExpenseDebt",
                        "InterestExpenseNonoperating", "InterestAndDebtExpense",
                        "InterestIncomeExpenseNet"],
    "depreciationAndAmortization": ["DepreciationDepletionAndAmortization",
                                    "DepreciationAmortizationAndAccretionNet",
                                    "DepreciationAndAmortization", "Depreciation",
                                    "DepreciationNonproduction"],
    "capitalExpenditure": ["PaymentsToAcquirePropertyPlantAndEquipment",
                           "PaymentsToAcquireProductiveAssets",
                           "PaymentsForCapitalImprovements"],
    "operatingCashFlow": ["NetCashProvidedByUsedInOperatingActivities",
                          "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
}
EPS_TAGS = ["EarningsPerShareDiluted"]
EPS_BASIC_TAGS = ["EarningsPerShareBasic"]
SHARE_TAGS = ["WeightedAverageNumberOfDilutedSharesOutstanding",
              "WeightedAverageNumberOfDilutedSharesOutstandingBasicAndDiluted"]
EQUITY_TAGS = ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
# 总债务分三段组装，避免重复计算（见文件头第 4 条）：
#   非流动 = LongTermDebtNoncurrent，缺失时用 LongTermDebt − LongTermDebtCurrent
#   流动   = DebtCurrent（已含长债当期部分），缺失时用 LongTermDebtCurrent + 短期借款
#   租赁   = 融资租赁负债（yfinance 的 Total Debt 也含它）
DEBT_LT_TOTAL_TAGS = ["LongTermDebt"]                    # 含当期到期部分的聚合值
# Exxon 不打 LongTermDebt/Noncurrent，只打 …AndCapitalLeaseObligations（含资本租赁）
DEBT_LT_NONCURRENT_TAGS = ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"]
DEBT_LT_CURRENT_TAGS = ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"]
DEBT_CURRENT_TOTAL_TAGS = ["DebtCurrent"]                # 已含长债当期部分
DEBT_SHORT_TERM_TAGS = ["ShortTermBorrowings", "CommercialPaper", "OtherShortTermBorrowings"]
# 上面全都取不到时的兜底：保险/金融公司常只打这一个标签（ACGL 就是）
DEBT_FALLBACK_TAGS = ["SeniorLongTermNotes", "SeniorNotes", "NotesPayable",
                      "LongTermNotesPayable", "DebtLongtermAndShorttermCombinedAmount"]
# 口径说明：总债务 = 有息借款。EDGAR 里融资租赁负债没有可靠的统一标签（微软只打
# 到期表、不打 FinanceLeaseLiability），所以一般不含——这会使债务/FCF 比 yfinance
# 口径略低（微软 431 亿 vs 606 亿、英伟达 85 亿 vs 110 亿，差额都是融资租赁）。
# 例外：只打 …AndCapitalLeaseObligations 的公司（如 Exxon）里含资本租赁，无法剥离。
# 已知缺口：**银行的长期债务在 us-gaap 顶层没有对应标签**（JPM 只打
# ShortTermBorrowings 648 亿，实际总债务约 5000 亿）。银行本就过不了毛利率筛，不予处理。


def _iso(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


class EDGARClient(YFClient):
    """财报走 SEC EDGAR，其余（报价/汇率/国债/成分股）继承 YFClient。"""

    def __init__(self, cache: Cache, user_agent: str, years: int = 10, pause: float = 0.12):
        super().__init__(cache)
        if not user_agent or "@" not in user_agent:
            raise FMPError("EDGAR 要求 User-Agent 含联系邮箱，请在 config.yaml 的 edgar.user_agent 配置")
        self.ua = {"User-Agent": user_agent}
        self.years = years
        self.sec_pause = pause          # SEC 允许 ≤10 req/s
        self._cik_map: dict[str, int] | None = None
        self._facts_cache: dict[str, dict] = {}

    # ---------- 底层请求 ----------
    def _get_json(self, url: str):
        last = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=self.ua)
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except Exception as e:      # noqa: BLE001
                last = e
                time.sleep(1.0 + attempt)
        raise FMPError(f"EDGAR 请求失败 {url}: {last}")

    def _cik(self, symbol: str) -> int:
        if symbol in TICKER_CIK_OVERRIDES:
            return TICKER_CIK_OVERRIDES[symbol]
        if self._cik_map is None:
            cached = self.cache.get("edgar_cik_map", "universe")
            if cached is None:
                raw = self._get_json(SEC_TICKERS_URL)
                cached = {v["ticker"]: v["cik_str"] for v in raw.values()}
                self.cache.put("edgar_cik_map", "universe", cached)
            self._cik_map = cached
        for variant in (symbol, symbol.replace("-", "."), symbol.replace("-", "")):
            if variant in self._cik_map:
                return self._cik_map[variant]
        raise FMPError(f"EDGAR 无 {symbol} 的 CIK 映射（可能已退市或为外国发行人）")

    # ---------- XBRL 事实抽取 ----------
    @staticmethod
    def _facts_for_tag(us_gaap: dict, tag: str, unit: str,
                       instant: bool) -> dict[str, tuple[float, str]]:
        """返回 {期末日期: (数值, 申报日期)}。申报日期用于拆股复权，不能丢。"""
        node = us_gaap.get(tag)
        if not node:
            return {}
        vals = node.get("units", {}).get(unit)
        if not vals:
            return {}
        out: dict[str, tuple[float, str]] = {}
        for x in vals:
            if not str(x.get("form", "")).startswith("10-K") or not x.get("end"):
                continue
            if instant:
                if x.get("start"):
                    continue
            else:
                if not x.get("start"):
                    continue
                if not 330 <= (_iso(x["end"]) - _iso(x["start"])).days <= 400:
                    continue
            prev = out.get(x["end"])
            if prev is None or x.get("filed", "") > prev[1]:   # 重述取最新申报
                out[x["end"]] = (x["val"], x.get("filed", ""))
        return out

    def _merge_tags(self, us_gaap: dict, tags: list[str], unit: str = "USD",
                    instant: bool = False) -> dict[str, tuple[float, str]]:
        """逐年回退：按优先级依次填补尚缺的年份。值保留 (数值, 申报日期)。"""
        merged: dict[str, tuple[float, str]] = {}
        for tag in tags:
            for end, pair in self._facts_for_tag(us_gaap, tag, unit, instant).items():
                merged.setdefault(end, pair)
        return merged

    @staticmethod
    def _vals(merged: dict[str, tuple[float, str]]) -> dict[str, float]:
        """丢掉申报日期，只留数值（非每股口径的字段用）。"""
        return {end: v for end, (v, _) in merged.items()}

    def _sum_tags(self, us_gaap: dict, tags: list[str]) -> dict[str, float]:
        """把多个时点标签逐年相加（报了哪项加哪项）。"""
        out: dict[str, float] = {}
        for tag in tags:
            for end, (val, _) in self._facts_for_tag(us_gaap, tag, "USD", instant=True).items():
                out[end] = out.get(end, 0.0) + val
        return out

    def _total_debt(self, us_gaap: dict) -> dict[str, float]:
        """分层组装总债务，避免重复计算（见文件头第 4 条）。"""
        lt_total = self._vals(self._merge_tags(us_gaap, DEBT_LT_TOTAL_TAGS, instant=True))
        lt_noncur = self._vals(self._merge_tags(us_gaap, DEBT_LT_NONCURRENT_TAGS, instant=True))
        lt_cur = self._vals(self._merge_tags(us_gaap, DEBT_LT_CURRENT_TAGS, instant=True))
        cur_total = self._vals(self._merge_tags(us_gaap, DEBT_CURRENT_TOTAL_TAGS, instant=True))
        short = self._sum_tags(us_gaap, DEBT_SHORT_TERM_TAGS)
        fallback = self._vals(self._merge_tags(us_gaap, DEBT_FALLBACK_TAGS, instant=True))

        ends = set(lt_total) | set(lt_noncur) | set(lt_cur) | set(cur_total) | set(short)
        debt: dict[str, float] = {}
        for e in ends:
            if e in lt_noncur:
                noncurrent = lt_noncur[e]
            elif e in lt_total:
                noncurrent = lt_total[e] - lt_cur.get(e, 0.0)   # LongTermDebt 含当期到期部分
            else:
                noncurrent = 0.0
            if e in cur_total:
                current = cur_total[e]                          # DebtCurrent 已含长债当期部分
            else:
                current = lt_cur.get(e, 0.0) + short.get(e, 0.0)
            debt[e] = noncurrent + current
        for e, v in fallback.items():       # 主标签一个都没有的年份才用兜底
            debt.setdefault(e, v)
        return debt

    def _splits(self, symbol: str) -> list[tuple[str, float]]:
        cached = self.cache.get(f"splits:{symbol}", "statements")
        if cached is None:
            try:
                from .yf_client import _yfinance
                sp = _yfinance().Ticker(symbol).splits
                cached = ([[d.date().isoformat(), float(r)] for d, r in sp.items()]
                          if sp is not None and len(sp) else [])
            except Exception:           # noqa: BLE001
                cached = []
            self.cache.put(f"splits:{symbol}", "statements", cached)
        return [(d, r) for d, r in cached]

    def _split_factor_since_filing(self, splits, filed: str) -> float:
        """只累乘「该事实申报之后」发生的拆股。

        申报当时的报表已经反映了那之前的所有拆股，所以按财年末算会重复复权。
        EPS 需除以该因子、股本需乘以该因子，才能换算到今天的口径。
        """
        if not filed:
            return 1.0
        f = 1.0
        for day, ratio in splits:
            if _iso(day) > _iso(filed):
                f *= ratio
        return f

    # ---------- 三张表 ----------
    def _statements(self, symbol: str) -> dict:
        if symbol in self._facts_cache:
            return self._facts_cache[symbol]
        cached = self.cache.get(f"edgar_statements:{symbol}", "statements")
        if cached is not None:
            self._facts_cache[symbol] = cached
            return cached

        cik = self._cik(symbol)
        facts = self._get_json(SEC_FACTS_URL.format(cik=cik))
        time.sleep(self.sec_pause)
        us = facts.get("facts", {}).get("us-gaap")
        if not us:
            namespaces = list(facts.get("facts", {})) or ["无"]
            raise FMPError(
                f"EDGAR 无 {symbol} 的 us-gaap 事实：CIK {cik} = "
                f"{facts.get('entityName', '?')}，仅有 {namespaces}。"
                f"若该实体只有 ffd，说明公司做了控股重组、历史挂在旧 CIK 下，"
                f"请在 TICKER_CIK_OVERRIDES 里补一条")

        dur = {f: self._vals(self._merge_tags(us, tags))
               for f, tags in DURATION_TAGS.items()}
        # 每股口径的字段要保留申报日期，用于拆股复权
        eps_d = self._merge_tags(us, EPS_TAGS, unit="USD/shares")
        eps_b = self._merge_tags(us, EPS_BASIC_TAGS, unit="USD/shares")
        shares = self._merge_tags(us, SHARE_TAGS, unit="shares")
        equity = self._vals(self._merge_tags(us, EQUITY_TAGS, instant=True))
        debt = self._total_debt(us)

        if not dur["netIncome"]:
            raise FMPError(f"EDGAR 无 {symbol} 的净利润年度事实")
        ends = sorted(dur["netIncome"], reverse=True)[:self.years]
        splits = self._splits(symbol)

        income, balance, cashflow = [], [], []
        for e in ends:
            rev = dur["revenue"].get(e)
            gp = dur["grossProfit"].get(e)
            if gp is None and rev is not None and dur["costOfRevenue"].get(e) is not None:
                gp = rev - dur["costOfRevenue"][e]      # Alphabet/FactSet 不报毛利
            da = dur["depreciationAndAmortization"].get(e)
            op = dur["operatingIncome"].get(e)
            cfo = dur["operatingCashFlow"].get(e)
            capex = dur["capitalExpenditure"].get(e)
            fcf = (cfo - capex) if (cfo is not None and capex is not None) else None

            def _adj(merged, e=e, invert=False):
                """按该事实的申报日期做拆股复权。invert=True 用于股本（乘以因子）。"""
                if e not in merged:
                    return None
                val, filed = merged[e]
                k = self._split_factor_since_filing(splits, filed)
                return val * k if invert else val / k

            income.append({
                "calendarYear": e[:4], "date": e,
                "revenue": rev, "grossProfit": gp, "operatingIncome": op,
                "netIncome": dur["netIncome"].get(e),
                "epsDiluted": _adj(eps_d),
                "eps": _adj(eps_b),
                "interestExpense": dur["interestExpense"].get(e),
                "ebitda": (op + da) if (op is not None and da is not None) else None,
                "weightedAverageShsOutDil": _adj(shares, invert=True),
            })
            balance.append({
                "calendarYear": e[:4], "date": e,
                "totalStockholdersEquity": equity.get(e),
                "totalDebt": debt.get(e),
            })
            cashflow.append({
                "calendarYear": e[:4], "date": e,
                "netIncome": dur["netIncome"].get(e),
                "depreciationAndAmortization": da,
                "capitalExpenditure": -capex if capex is not None else None,  # FMP 口径为负
                "freeCashFlow": fcf,
            })

        profile = self._profile_from_sec(symbol, cik, facts.get("entityName", symbol))
        result = {"income": income, "balance": balance, "cashflow": cashflow,
                  "profile": profile}
        self.cache.put(f"edgar_statements:{symbol}", "statements", result)
        self._facts_cache[symbol] = result
        return result

    def _profile_from_sec(self, symbol: str, cik: int, entity_name: str) -> dict:
        cached = self.cache.get(f"edgar_profile:{symbol}", "statements")
        if cached is not None:
            return cached
        try:
            sub = self._get_json(SEC_SUBMISSIONS_URL.format(cik=cik))
            time.sleep(self.sec_pause)
            profile = {"companyName": sub.get("name") or entity_name,
                       "sector": sub.get("sicDescription") or "—"}
        except Exception:               # noqa: BLE001
            profile = {"companyName": entity_name, "sector": "—"}
        # SEC 只有 SIC 描述（"Services-Computer Programming…"），与页面按 GICS 行业
        # 分组的展示习惯不符。若本地还留着 yfinance 抓过的行业，优先沿用。
        yf_cached = self.cache.get(f"yf_statements:{symbol}", "statements")
        if yf_cached:
            yf_profile = yf_cached.get("profile") or {}
            if yf_profile.get("companyName"):
                profile["companyName"] = yf_profile["companyName"]
            if yf_profile.get("sector") and yf_profile["sector"] != "—":
                profile["sector"] = yf_profile["sector"]
        self.cache.put(f"edgar_profile:{symbol}", "statements", profile)
        return profile

    def profile(self, symbol: str) -> dict:
        return self._statements(symbol)["profile"]
