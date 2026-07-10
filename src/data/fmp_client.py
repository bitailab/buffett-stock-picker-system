"""Financial Modeling Prep API 客户端（新版 stable API，2025-08 后注册的 key 只能用这版）。

免费档限制与应对：
- 每天 250 次请求：所有响应先查 SQLite 缓存，未命中才真正调用；达到当日上限抛出
  BudgetExhausted，调用方保存进度退出，次日运行自动从断点继续。
- 财报 limit 最多 5 年（付费档可到 10 年，config 的 plan 设为 starter 即放开）。
- 外汇报价、S&P 500 成分股、国债收益率端点不对免费档开放：
  汇率改用 Frankfurter（欧洲央行数据，免费无 key），成分股改用 GitHub 开源数据集，
  这两项不占 FMP 配额；国债收益率拿不到时估值层自动跳过该校验。
"""
import datetime as dt
import time

import requests

from .cache import Cache

BASE_URL = "https://financialmodelingprep.com/stable"
FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"
SP500_CSV_URL = ("https://raw.githubusercontent.com/datasets/"
                 "s-and-p-500-companies/main/data/constituents.csv")


class BudgetExhausted(Exception):
    """当日 API 调用配额用尽。"""


class FMPError(Exception):
    """FMP API 返回错误。"""


class FMPClient:
    def __init__(self, api_key: str, cache: Cache, plan: str = "free",
                 daily_call_budget: int = 240):
        self.api_key = api_key
        self.cache = cache
        self.plan = plan
        self.daily_call_budget = daily_call_budget
        self.statement_limit = 5 if plan == "free" else 10
        self.session = requests.Session()

    # ---------- 底层请求 ----------
    def _today(self) -> str:
        return dt.date.today().isoformat()

    def calls_remaining(self) -> int:
        if self.plan != "free":
            return 10 ** 9
        return self.daily_call_budget - self.cache.calls_today(self._today())

    def _request(self, path: str, category: str, params: dict | None = None,
                 cache_key: str | None = None):
        key = cache_key or f"{path}?{sorted((params or {}).items())}"
        cached = self.cache.get(key, category)
        if cached is not None:
            return cached

        if self.plan == "free" and self.calls_remaining() <= 0:
            raise BudgetExhausted(
                f"今日 API 配额已用完（{self.daily_call_budget} 次），明天再运行即可从断点继续。"
            )

        query = dict(params or {})
        query["apikey"] = self.api_key
        for attempt in range(3):
            try:
                resp = self.session.get(f"{BASE_URL}/{path}", params=query, timeout=30)
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            break

        self.cache.record_call(self._today())
        if resp.status_code != 200:
            raise FMPError(f"FMP {path} 返回 HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError:
            raise FMPError(f"FMP {path} 返回非 JSON 响应: {resp.text[:200]}")
        if isinstance(data, dict) and "Error Message" in data:
            raise FMPError(f"FMP {path}: {data['Error Message']}")

        self.cache.put(key, category, data)
        return data

    def _external(self, url: str, category: str, cache_key: str,
                  params: dict | None = None):
        """FMP 之外的免费数据源，不占 FMP 配额。"""
        cached = self.cache.get(cache_key, category)
        if cached is not None:
            return cached
        resp = self.session.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            raise FMPError(f"{url} 返回 HTTP {resp.status_code}")
        data = resp.json() if "json" in resp.headers.get("content-type", "") else resp.text
        self.cache.put(cache_key, category, data)
        return data

    # ---------- 业务端点 ----------
    def sp500_constituents(self) -> list[dict]:
        """S&P 500 成分股（GitHub 开源数据集，免费档 FMP 不开放此端点）。"""
        csv_text = self._external(SP500_CSV_URL, "universe", "sp500_csv")
        lines = csv_text.strip().split("\n")
        symbols = []
        for line in lines[1:]:
            sym = line.split(",")[0].strip()
            if sym:
                # 数据集用 BRK.B 格式，FMP 用 BRK-B
                symbols.append({"symbol": sym.replace(".", "-")})
        return symbols

    def profile(self, symbol: str) -> dict:
        data = self._request("profile", "profile", params={"symbol": symbol},
                             cache_key=f"profile:{symbol}")
        return data[0] if data else {}

    def income_statements(self, symbol: str) -> list[dict]:
        return self._request(
            "income-statement", "statements",
            params={"symbol": symbol, "period": "annual", "limit": self.statement_limit},
            cache_key=f"income:{symbol}",
        )

    def balance_sheets(self, symbol: str) -> list[dict]:
        return self._request(
            "balance-sheet-statement", "statements",
            params={"symbol": symbol, "period": "annual", "limit": self.statement_limit},
            cache_key=f"balance:{symbol}",
        )

    def cash_flows(self, symbol: str) -> list[dict]:
        return self._request(
            "cash-flow-statement", "statements",
            params={"symbol": symbol, "period": "annual", "limit": self.statement_limit},
            cache_key=f"cashflow:{symbol}",
        )

    def quote(self, symbol: str) -> dict:
        data = self._request("quote", "quote", params={"symbol": symbol},
                             cache_key=f"quote:{symbol}")
        return data[0] if data else {}

    def sgd_usd_rate(self) -> float:
        """1 SGD 兑多少 USD（Frankfurter/欧洲央行，免费无 key，不占 FMP 配额）。"""
        data = self._external(FRANKFURTER_URL, "forex", "fx:SGDUSD",
                              params={"base": "SGD", "symbols": "USD"})
        rate = (data.get("rates") or {}).get("USD") if isinstance(data, dict) else None
        if not rate:
            raise FMPError("无法获取 SGD/USD 汇率")
        return float(rate)

    def treasury_10y(self) -> float | None:
        """美国 10 年期国债收益率（%）。免费档不开放该端点，拿不到返回 None，
        估值层会跳过盈利收益率 vs 国债的校验。"""
        try:
            data = self._request("treasury-rates", "treasury", cache_key="treasury10y")
        except (FMPError, BudgetExhausted):
            return None
        if isinstance(data, list) and data:
            val = data[0].get("year10")
            return float(val) if val else None
        return None
