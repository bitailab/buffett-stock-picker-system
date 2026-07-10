#!/usr/bin/env python3
"""交互式 Web 页面：python webapp.py 后浏览器打开 http://localhost:8600

- 仪表盘：扫描结果排行、可买入名单，可触发后台重扫
- 定投：输入每月预算(SGD)，给出本月分配
- 建仓：输入一次性金额(USD)和投资期限，构建预期收益最高的组合
- 持仓：查看/录入实际买入
"""
import datetime as dt
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from flask import Flask, jsonify, render_template, request

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from main import load_config, get_context, _get_fx
from src.data.universe import get_universe
from src.strategy.scoring import load_scores
from src.portfolio.tracker import PortfolioTracker
from src.portfolio.allocator import build_plan
from src.portfolio.builder import build_lumpsum
from src.data.fmp_client import BudgetExhausted, FMPError

app = Flask(__name__, template_folder="src/webapp/templates")
_scan_proc: subprocess.Popen | None = None


def _ctx():
    cfg = load_config()
    client, conn = get_context(cfg)
    return cfg, client, conn


@app.route("/")
def index():
    return render_template("app.html")


@app.route("/api/summary")
def api_summary():
    cfg, client, conn = _ctx()
    scores = load_scores(conn)
    universe_size = len(get_universe(client))
    buyable = [s for s in scores if s.get("buyable")]
    quality = [s for s in scores if s.get("quality_passed")]
    last_scan = max((s["scanned_at"] for s in scores), default=None)
    try:
        fx = 1.0 / client.sgd_usd_rate()
    except (FMPError, BudgetExhausted):
        fx = None
    return jsonify({
        "scanned": len(scores), "universe": universe_size,
        "quality_passed": len(quality), "buyable": len(buyable),
        "last_scan": last_scan, "fx_sgd_per_usd": round(fx, 4) if fx else None,
        "monthly_budget_sgd": cfg["investment"]["monthly_budget_sgd"],
    })


@app.route("/api/ranking")
def api_ranking():
    _, _, conn = _ctx()
    scores = load_scores(conn)
    # 默认返回全部（约 500 只）：翻页与搜索在前端做，避免反复请求。
    # 仍支持 ?limit=N 截断（老接口兼容）。
    limit = request.args.get("limit")
    if limit is not None:
        scores = scores[: int(limit)]
    out = []
    for s in scores:
        out.append({k: s.get(k) for k in (
            "symbol", "name", "sector", "price", "total_score", "quality_score",
            "intrinsic_value", "margin_of_safety", "quality_passed", "buyable")}
            | {"checks": s.get("checks")})
    return jsonify(out)


@app.route("/api/dca")
def api_dca():
    cfg, client, conn = _ctx()
    budget = float(request.args.get("budget_sgd",
                                    cfg["investment"]["monthly_budget_sgd"]))
    scores = load_scores(conn)
    tracker = PortfolioTracker(conn)
    holdings = tracker.holdings(_prices(client, tracker))
    fx = _get_fx(client)
    plan = build_plan(scores, holdings, budget, fx, cfg)
    return jsonify(asdict(plan))


@app.route("/api/lumpsum")
def api_lumpsum():
    _, _, conn = _ctx()
    amount = float(request.args.get("amount_usd", 10000))
    years = float(request.args.get("years", 1))
    plan = build_lumpsum(load_scores(conn), amount, years)
    return jsonify(asdict(plan))


def _prices(client, tracker) -> dict:
    prices = {}
    for h in tracker.holdings():
        try:
            q = client.quote(h.symbol)
            if q.get("price"):
                prices[h.symbol] = q["price"]
        except (FMPError, BudgetExhausted):
            break
    return prices


@app.route("/api/holdings")
def api_holdings():
    _, client, conn = _ctx()
    tracker = PortfolioTracker(conn)
    holdings = tracker.holdings(_prices(client, tracker))
    return jsonify({"holdings": [asdict(h) for h in holdings],
                    "trades": tracker.trades()})


@app.route("/api/buy", methods=["POST"])
def api_buy():
    _, client, conn = _ctx()
    d = request.get_json(force=True)
    fx = d.get("fx") or round(1.0 / _get_fx(client), 4)
    trade_id = PortfolioTracker(conn).record_buy(
        d["symbol"], float(d["shares"]), float(d["price"]),
        float(fx), note=d.get("note", ""))
    return jsonify({"ok": True, "trade_id": trade_id, "fx": fx})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    global _scan_proc
    if _scan_proc and _scan_proc.poll() is None:
        return jsonify({"ok": False, "error": "扫描已在进行中"})
    _scan_proc = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "main.py"), "scan"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({"ok": True})


@app.route("/api/scan/status")
def api_scan_status():
    _, client, conn = _ctx()
    running = _scan_proc is not None and _scan_proc.poll() is None
    n = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    return jsonify({"running": running, "scanned": n,
                    "universe": len(get_universe(client))})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8600, debug=False)
