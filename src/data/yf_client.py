"""yfinance 数据客户端 —— 与 FMPClient 接口一致，策略层无需感知数据源。

免费、覆盖全部美股、无每日配额（年报深度约 5 年）。
把 yfinance 的 DataFrame 转成 FMP 风格的字段名，复用同一套 SQLite 缓存
（财报 90 天、报价 12 小时），重复运行不会反复请求 Yahoo。
"""
import math
import time

import requests

from .cache import Cache
from .fmp_client import FMPError, SP500_CSV_URL, FRANKFURTER_URL

# yfinance 导入较慢，延迟到实际使用时
_yf = None


def _yfinance():
    global _yf
    if _yf is None:
        import yfinance
        _yf = yfinance
    return _yf


def _v(df, field, col):
    """从 DataFrame 取值，缺失/NaN 返回 None。"""
    if df is None or field not in df.index:
        return None
    val = df.loc[field, col]
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    return float(val)


def _first(df, fields, col):
    for f in fields:
        val = _v(df, f, col)
        if val is not None:
            return val
    return None


class YFClient:
    """接口与 FMPClient 对齐：profile / income_statements / balance_sheets /
    cash_flows / quote / sgd_usd_rate / treasury_10y / sp500_constituents / calls_remaining"""

    def __init__(self, cache: Cache, pause: float = 0.4):
        self.cache = cache
        self.pause = pause          # 每只股票抓取后的间隔，避免触发 Yahoo 限流
        self.session = requests.Session()
        self._statement_cache: dict[str, dict] = {}   # 单次运行内的内存缓存

    def calls_remaining(self) -> int:
        return 10 ** 9   # 无每日配额

    # ---------- 财报（一次抓齐三张表并落缓存） ----------
    def _statements(self, symbol: str) -> dict:
        if symbol in self._statement_cache:
            return self._statement_cache[symbol]
        cached = self.cache.get(f"yf_statements:{symbol}", "statements")
        if cached is not None:
            self._statement_cache[symbol] = cached
            return cached

        t = _yfinance().Ticker(symbol)
        inc_df, bal_df, cf_df = t.income_stmt, t.balance_sheet, t.cashflow
        if inc_df is None or inc_df.empty:
            raise FMPError(f"yfinance 无 {symbol} 财报数据")

        income, balance, cashflow = [], [], []
        for col in inc_df.columns:   # 最新在前，与 FMP 一致
            year = str(col.year)
            ni = _v(inc_df, "Net Income", col)
            income.append({
                "calendarYear": year,
                "revenue": _v(inc_df, "Total Revenue", col),
                "grossProfit": _v(inc_df, "Gross Profit", col),
                "netIncome": ni,
                "epsDiluted": _v(inc_df, "Diluted EPS", col),
                "eps": _v(inc_df, "Basic EPS", col),
                "operatingIncome": _first(inc_df, ["Operating Income", "EBIT"], col),
                "interestExpense": _v(inc_df, "Interest Expense", col),
                "ebitda": _v(inc_df, "EBITDA", col),
                "weightedAverageShsOutDil": _first(
                    inc_df, ["Diluted Average Shares", "Basic Average Shares"], col),
            })
            bal_col = col if (bal_df is not None and col in bal_df.columns) else None
            balance.append({
                "calendarYear": year,
                "totalDebt": _v(bal_df, "Total Debt", bal_col) if bal_col is not None else None,
                "totalStockholdersEquity": (_first(
                    bal_df, ["Stockholders Equity", "Common Stock Equity"], bal_col)
                    if bal_col is not None else None),
            })
            cf_col = col if (cf_df is not None and col in cf_df.columns) else None
            cashflow.append({
                "calendarYear": year,
                "netIncome": ni,   # FCF 转化率用；与利润表口径一致
                "freeCashFlow": _v(cf_df, "Free Cash Flow", cf_col) if cf_col is not None else None,
                "depreciationAndAmortization": (_first(
                    cf_df, ["Depreciation Amortization Depletion",
                            "Depreciation And Amortization"], cf_col)
                    if cf_col is not None else None),
                "capitalExpenditure": _v(cf_df, "Capital Expenditure", cf_col) if cf_col is not None else None,
            })

        # 报价/概况顺手一起抓，省一次网络请求
        try:
            fi = t.fast_info
            quote = {"price": float(fi.last_price) if fi.last_price else None,
                     "marketCap": float(fi.market_cap) if fi.market_cap else None,
                     "sharesOutstanding": float(fi.shares) if fi.shares else None}
        except Exception:
            quote = {}
        try:
            info = t.info or {}
            profile = {"companyName": info.get("longName") or info.get("shortName") or symbol,
                       "sector": info.get("sector", "—")}
        except Exception:
            profile = {"companyName": symbol, "sector": "—"}

        result = {"income": income, "balance": balance, "cashflow": cashflow,
                  "quote": quote, "profile": profile}
        self.cache.put(f"yf_statements:{symbol}", "statements", result)
        self.cache.put(f"quote:{symbol}", "quote", [quote])
        self._statement_cache[symbol] = result
        time.sleep(self.pause)
        return result

    # ---------- 与 FMPClient 对齐的接口 ----------
    def profile(self, symbol: str) -> dict:
        return self._statements(symbol)["profile"]

    def income_statements(self, symbol: str) -> list[dict]:
        return self._statements(symbol)["income"]

    def balance_sheets(self, symbol: str) -> list[dict]:
        return self._statements(symbol)["balance"]

    def cash_flows(self, symbol: str) -> list[dict]:
        return self._statements(symbol)["cashflow"]

    def quote(self, symbol: str) -> dict:
        cached = self.cache.get(f"quote:{symbol}", "quote")
        if cached:
            return cached[0]
        try:
            fi = _yfinance().Ticker(symbol).fast_info
            quote = {"price": float(fi.last_price) if fi.last_price else None,
                     "marketCap": float(fi.market_cap) if fi.market_cap else None,
                     "sharesOutstanding": float(fi.shares) if fi.shares else None}
        except Exception as e:
            raise FMPError(f"yfinance 获取 {symbol} 报价失败: {e}")
        self.cache.put(f"quote:{symbol}", "quote", [quote])
        return quote

    def sp500_constituents(self) -> list[dict]:
        cached = self.cache.get("sp500_csv", "universe")
        if cached is None:
            resp = self.session.get(SP500_CSV_URL, timeout=30)
            if resp.status_code != 200:
                raise FMPError(f"获取 S&P 500 成分股失败 HTTP {resp.status_code}")
            cached = resp.text
            self.cache.put("sp500_csv", "universe", cached)
        symbols = []
        for line in cached.strip().split("\n")[1:]:
            sym = line.split(",")[0].strip()
            if sym:
                symbols.append({"symbol": sym.replace(".", "-")})
        return symbols

    def sgd_usd_rate(self) -> float:
        cached = self.cache.get("fx:SGDUSD", "forex")
        if cached is None:
            resp = self.session.get(FRANKFURTER_URL,
                                    params={"base": "SGD", "symbols": "USD"}, timeout=30)
            if resp.status_code != 200:
                raise FMPError("无法获取 SGD/USD 汇率")
            cached = resp.json()
            self.cache.put("fx:SGDUSD", "forex", cached)
        rate = (cached.get("rates") or {}).get("USD")
        if not rate:
            raise FMPError("无法获取 SGD/USD 汇率")
        return float(rate)

    def treasury_10y(self) -> float | None:
        """10 年期美债收益率，取 Yahoo ^TNX 指数。"""
        cached = self.cache.get("treasury10y_yf", "treasury")
        if cached is not None:
            return cached
        try:
            val = _yfinance().Ticker("^TNX").fast_info.last_price
            if not val:
                return None
            val = float(val)
            if val > 20:   # ^TNX 有时按收益率×10 报价
                val /= 10
            self.cache.put("treasury10y_yf", "treasury", val)
            return val
        except Exception:
            return None
