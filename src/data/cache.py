"""SQLite 缓存层：财报数据按 TTL 缓存，避免重复消耗 FMP 免费配额。"""
import json
import sqlite3
import time
from pathlib import Path

# 各类数据的缓存有效期（秒）
TTL = {
    "statements": 90 * 86400,   # 财报（年报）：90 天
    "profile": 90 * 86400,      # 公司概况：90 天
    "universe": 30 * 86400,     # S&P 500 成分股：30 天
    "quote": 12 * 3600,         # 报价：12 小时
    "forex": 12 * 3600,         # 汇率：12 小时
    "treasury": 7 * 86400,      # 国债收益率：7 天
}


class Cache:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS api_cache (
                   key TEXT PRIMARY KEY,
                   category TEXT NOT NULL,
                   payload TEXT NOT NULL,
                   fetched_at REAL NOT NULL
               )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS call_budget (
                   day TEXT PRIMARY KEY,
                   calls INTEGER NOT NULL DEFAULT 0
               )"""
        )
        self.conn.commit()

    # ---------- API 响应缓存 ----------
    def get(self, key: str, category: str):
        row = self.conn.execute(
            "SELECT payload, fetched_at FROM api_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        payload, fetched_at = row
        if time.time() - fetched_at > TTL.get(category, 86400):
            return None
        return json.loads(payload)

    def put(self, key: str, category: str, payload):
        self.conn.execute(
            "INSERT OR REPLACE INTO api_cache (key, category, payload, fetched_at) VALUES (?, ?, ?, ?)",
            (key, category, json.dumps(payload), time.time()),
        )
        self.conn.commit()

    # ---------- 每日调用配额 ----------
    def calls_today(self, day: str) -> int:
        row = self.conn.execute(
            "SELECT calls FROM call_budget WHERE day = ?", (day,)
        ).fetchone()
        return row[0] if row else 0

    def record_call(self, day: str):
        self.conn.execute(
            "INSERT INTO call_budget (day, calls) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
            (day,),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
