"""第三层：综合巴菲特评分（0-100），并把打分结果落库供报告使用。"""
import datetime as dt
import json
import sqlite3
from dataclasses import asdict, dataclass

from .quality import QualityResult
from .valuation import ValuationResult


@dataclass
class StockScore:
    symbol: str
    name: str
    sector: str
    price: float | None
    total_score: float          # 0-100 综合分
    quality_score: float
    valuation_score: float
    margin_of_safety: float | None
    intrinsic_value: float | None
    quality_passed: bool        # 核心质量标准全过
    buyable: bool               # 质量达标 且 安全边际达标
    checks: list[dict]          # 质量明细（Check 的 dict 列表）
    valuation: dict             # ValuationResult 的 dict
    scanned_at: str


def compose_score(symbol: str, name: str, sector: str,
                  q: QualityResult, v: ValuationResult, cfg: dict) -> StockScore:
    """cfg 为完整配置（需要 scoring 和 valuation 两段）。"""
    mos = v.margin_of_safety
    # 安全边际 50% 及以上拿满分，负值为 0
    valuation_score = max(0.0, min(mos or 0.0, 50.0)) / 50.0 * 100
    total = (q.score * cfg["scoring"]["quality_weight"]
             + valuation_score * cfg["scoring"]["valuation_weight"])
    buyable = (q.passed_all_core
               and mos is not None
               and mos >= cfg["valuation"]["margin_of_safety_min"])
    return StockScore(
        symbol=symbol, name=name, sector=sector, price=v.price,
        total_score=round(total, 1),
        quality_score=q.score,
        valuation_score=round(valuation_score, 1),
        margin_of_safety=round(mos, 1) if mos is not None else None,
        intrinsic_value=round(v.intrinsic_per_share, 2) if v.intrinsic_per_share else None,
        quality_passed=q.passed_all_core,
        buyable=buyable,
        checks=[asdict(c) for c in q.checks],
        valuation=asdict(v),
        scanned_at=dt.datetime.now().isoformat(timespec="seconds"),
    )


# ---------- 扫描结果持久化 ----------

def init_scores_table(conn: sqlite3.Connection):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scores (
               symbol TEXT PRIMARY KEY,
               payload TEXT NOT NULL,
               total_score REAL NOT NULL,
               buyable INTEGER NOT NULL,
               scanned_at TEXT NOT NULL
           )"""
    )
    conn.commit()


def save_score(conn: sqlite3.Connection, s: StockScore):
    conn.execute(
        "INSERT OR REPLACE INTO scores (symbol, payload, total_score, buyable, scanned_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (s.symbol, json.dumps(asdict(s)), s.total_score, int(s.buyable), s.scanned_at),
    )
    conn.commit()


def load_scores(conn: sqlite3.Connection) -> list[dict]:
    init_scores_table(conn)
    rows = conn.execute(
        "SELECT payload FROM scores ORDER BY total_score DESC"
    ).fetchall()
    return [json.loads(r[0]) for r in rows]
