"""持仓账本：记录每笔买入（股票、股数、美元价格、SGD/USD 汇率），计算成本与盈亏。"""
import datetime as dt
import sqlite3
from dataclasses import dataclass


@dataclass
class Holding:
    symbol: str
    shares: float
    cost_usd: float          # 总成本（USD）
    cost_sgd: float          # 总成本（SGD）
    avg_price_usd: float
    market_value_usd: float | None = None
    unrealized_pnl_usd: float | None = None
    unrealized_pnl_pct: float | None = None
    weight_pct: float | None = None


class PortfolioTracker:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS trades (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   symbol TEXT NOT NULL,
                   shares REAL NOT NULL,
                   price_usd REAL NOT NULL,
                   fx_sgd_per_usd REAL NOT NULL,
                   traded_at TEXT NOT NULL,
                   note TEXT DEFAULT ''
               )"""
        )
        self.conn.commit()

    def record_buy(self, symbol: str, shares: float, price_usd: float,
                   fx_sgd_per_usd: float, note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO trades (symbol, shares, price_usd, fx_sgd_per_usd, traded_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol.upper(), shares, price_usd, fx_sgd_per_usd,
             dt.datetime.now().isoformat(timespec="seconds"), note),
        )
        self.conn.commit()
        return cur.lastrowid

    def delete_trade(self, trade_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def trades(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, symbol, shares, price_usd, fx_sgd_per_usd, traded_at, note "
            "FROM trades ORDER BY traded_at"
        ).fetchall()
        cols = ["id", "symbol", "shares", "price_usd", "fx_sgd_per_usd", "traded_at", "note"]
        return [dict(zip(cols, r)) for r in rows]

    def holdings(self, prices: dict[str, float] | None = None) -> list[Holding]:
        """按股票聚合持仓；prices 提供现价时计算市值和盈亏。"""
        agg: dict[str, dict] = {}
        for t in self.trades():
            a = agg.setdefault(t["symbol"], {"shares": 0.0, "cost_usd": 0.0, "cost_sgd": 0.0})
            cost = t["shares"] * t["price_usd"]
            a["shares"] += t["shares"]
            a["cost_usd"] += cost
            a["cost_sgd"] += cost * t["fx_sgd_per_usd"]

        holdings = []
        for sym, a in sorted(agg.items()):
            if a["shares"] <= 0:
                continue
            h = Holding(
                symbol=sym, shares=a["shares"],
                cost_usd=a["cost_usd"], cost_sgd=a["cost_sgd"],
                avg_price_usd=a["cost_usd"] / a["shares"],
            )
            price = (prices or {}).get(sym)
            if price:
                h.market_value_usd = a["shares"] * price
                h.unrealized_pnl_usd = h.market_value_usd - a["cost_usd"]
                h.unrealized_pnl_pct = h.unrealized_pnl_usd / a["cost_usd"] * 100
            holdings.append(h)

        total_mv = sum(h.market_value_usd for h in holdings if h.market_value_usd)
        if total_mv:
            for h in holdings:
                if h.market_value_usd:
                    h.weight_pct = h.market_value_usd / total_mv * 100
        return holdings
