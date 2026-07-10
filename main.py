#!/usr/bin/env python3
"""巴菲特策略美股定投系统 —— CLI 入口。

用法：
  python main.py scan [--tickers AAPL,KO] [--limit 50] [--force]
  python main.py report
  python main.py buy AAPL 5 --price 230.50 [--fx 1.34] [--note "7月定投"]
  python main.py portfolio
  python main.py trades
  python main.py delete-trade 3
"""
import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.cache import Cache
from src.data.fmp_client import FMPClient, BudgetExhausted, FMPError
from src.data.universe import get_universe
from src.strategy.quality import evaluate_quality
from src.strategy.valuation import evaluate_valuation
from src.strategy.scoring import compose_score, init_scores_table, save_score, load_scores
from src.portfolio.tracker import PortfolioTracker
from src.portfolio.allocator import build_plan
from src.report.html_report import render_report

RESCAN_AFTER_DAYS = 25   # 一只股票扫描过后多少天内不重复扫描（除非 --force）


def load_config() -> dict:
    with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_context(cfg):
    db_path = str(PROJECT_ROOT / cfg["paths"]["db"])
    cache = Cache(db_path)
    if cfg.get("data_source", "yfinance") == "fmp":
        client = FMPClient(cfg["fmp"]["api_key"], cache,
                           plan=cfg["fmp"].get("plan", "free"),
                           daily_call_budget=cfg["fmp"].get("daily_call_budget", 240))
    else:
        from src.data.yf_client import YFClient
        client = YFClient(cache)
    conn = sqlite3.connect(db_path)
    init_scores_table(conn)
    return client, conn


# ============================================================
# scan
# ============================================================
def cmd_scan(args):
    cfg = load_config()
    if (cfg.get("data_source", "yfinance") == "fmp"
            and cfg["fmp"]["api_key"] in ("", "YOUR_FMP_API_KEY")):
        sys.exit("请先在 config.yaml 中填入 FMP API key（https://site.financialmodelingprep.com 免费注册）")
    client, conn = get_context(cfg)

    if args.tickers:
        symbols = [s.strip().upper() for s in args.tickers.split(",") if s.strip()]
    else:
        symbols = get_universe(client)
    if args.limit:
        symbols = symbols[: args.limit]

    # 断点续扫：跳过近期已扫描的股票
    recent_cutoff = (dt.datetime.now() - dt.timedelta(days=RESCAN_AFTER_DAYS)).isoformat()
    already = {r[0] for r in conn.execute(
        "SELECT symbol FROM scores WHERE scanned_at > ?", (recent_cutoff,))}
    todo = symbols if args.force else [s for s in symbols if s not in already]

    remaining = client.calls_remaining()
    quota_msg = f"；今日剩余 API 配额约 {remaining} 次" if remaining < 10 ** 6 else "（数据源无配额限制）"
    print(f"股票池 {len(symbols)} 只，其中 {len(symbols) - len(todo)} 只近期已扫描，"
          f"本次待扫 {len(todo)} 只{quota_msg}")

    treasury = None
    try:
        treasury = client.treasury_10y()
    except (BudgetExhausted, FMPError):
        pass

    done = failed = 0
    for sym in todo:
        try:
            profile = client.profile(sym)
            income = client.income_statements(sym)
            balance = client.balance_sheets(sym)
            cashflow = client.cash_flows(sym)
            quote = client.quote(sym)
        except BudgetExhausted as e:
            print(f"\n⏸  {e}")
            print(f"本次已完成 {done} 只（另有 {failed} 只数据异常），进度已保存。")
            return
        except FMPError as e:
            print(f"  ✗ {sym}: {e}")
            failed += 1
            continue

        price = quote.get("price")
        # 新版 quote 无 sharesOutstanding，用市值/价格推算，兜底用财报稀释股本
        shares_out = None
        if quote.get("marketCap") and price:
            shares_out = quote["marketCap"] / price
        if not shares_out and income:
            shares_out = income[0].get("weightedAverageShsOutDil")

        q = evaluate_quality(income, balance, cashflow, cfg["quality"])
        v = evaluate_valuation(income, cashflow, price, shares_out,
                               cfg["valuation"], treasury_10y=treasury)
        score = compose_score(
            sym,
            profile.get("companyName", sym),
            profile.get("sector", "—"),
            q, v, cfg,
        )
        save_score(conn, score)
        done += 1
        flag = "★可买入" if score.buyable else ("质量达标" if score.quality_passed else "")
        print(f"  ✓ {sym:<6} 综合分 {score.total_score:>5}  "
              f"安全边际 {score.margin_of_safety if score.margin_of_safety is not None else '—':>6}  {flag}")

    print(f"\n扫描完成：本次 {done} 只，异常 {failed} 只。运行 `python main.py report` 生成月报。")


# ============================================================
# report
# ============================================================
def cmd_report(args):
    cfg = load_config()
    client, conn = get_context(cfg)
    scores = load_scores(conn)
    if not scores:
        sys.exit("还没有扫描数据，请先运行 `python main.py scan`")

    fx = _get_fx(client)
    tracker = PortfolioTracker(conn)

    # 持仓现价（缓存 12 小时，失败则不算盈亏）
    prices = {}
    for h in tracker.holdings():
        try:
            quote = client.quote(h.symbol)
            if quote.get("price"):
                prices[h.symbol] = quote["price"]
        except (BudgetExhausted, FMPError):
            break
    holdings = tracker.holdings(prices)

    plan = build_plan(scores, holdings, cfg["investment"]["monthly_budget_sgd"], fx, cfg)
    path = render_report(plan, scores, holdings, str(PROJECT_ROOT / cfg["paths"]["reports_dir"]))

    print(f"月报已生成：{path}\n")
    if plan.hold_cash:
        print(f"本月建议：持币观望 —— {plan.hold_cash_reason}")
    else:
        print(f"本月买入建议（预算 {plan.budget_sgd:.0f} SGD ≈ {plan.budget_usd:.0f} USD，"
              f"汇率 1 USD = {plan.fx_sgd_per_usd} SGD）：")
        for s in plan.suggestions:
            print(f"  {s.symbol:<6} 现价 ${s.price:<9.2f} 整股 {s.whole_shares} 股"
                  f"（${s.whole_shares_usd:.2f}）或碎股 ${s.fractional_usd:.2f}"
                  f" ≈ S${s.alloc_sgd:.0f}  [综合分 {s.total_score}，安全边际 {s.margin_of_safety}%]")


def _get_fx(client) -> float:
    """1 SGD 兑多少 USD，API 失败时提示手动输入。"""
    try:
        return client.sgd_usd_rate()
    except (BudgetExhausted, FMPError) as e:
        print(f"获取汇率失败（{e}），使用近似值 0.74")
        return 0.74


# ============================================================
# buy / portfolio / trades
# ============================================================
def cmd_buy(args):
    cfg = load_config()
    client, conn = get_context(cfg)
    fx_sgd_per_usd = args.fx
    if fx_sgd_per_usd is None:
        fx_sgd_per_usd = 1.0 / _get_fx(client)
    tracker = PortfolioTracker(conn)
    trade_id = tracker.record_buy(args.symbol, args.shares, args.price,
                                  fx_sgd_per_usd, note=args.note or "")
    cost_usd = args.shares * args.price
    print(f"已记录买入 #{trade_id}：{args.symbol.upper()} {args.shares} 股 @ ${args.price:.2f} "
          f"= ${cost_usd:.2f} ≈ S${cost_usd * fx_sgd_per_usd:.2f}（汇率 1 USD = {fx_sgd_per_usd:.4f} SGD）")


def cmd_portfolio(args):
    cfg = load_config()
    client, conn = get_context(cfg)
    tracker = PortfolioTracker(conn)
    base = tracker.holdings()
    if not base:
        print("暂无持仓。用 `python main.py buy 代码 股数 --price 价格` 录入。")
        return
    prices = {}
    for h in base:
        try:
            quote = client.quote(h.symbol)
            if quote.get("price"):
                prices[h.symbol] = quote["price"]
        except (BudgetExhausted, FMPError):
            break
    holdings = tracker.holdings(prices)

    print(f"{'代码':<8}{'股数':>10}{'成本均价':>12}{'总成本USD':>14}{'总成本SGD':>14}"
          f"{'市值USD':>14}{'盈亏%':>9}{'占比%':>8}")
    total_cost = total_mv = total_sgd = 0.0
    for h in holdings:
        total_cost += h.cost_usd
        total_sgd += h.cost_sgd
        total_mv += h.market_value_usd or 0
        print(f"{h.symbol:<8}{h.shares:>10.4g}{h.avg_price_usd:>12.2f}{h.cost_usd:>14.2f}"
              f"{h.cost_sgd:>14.2f}"
              f"{(h.market_value_usd or 0):>14.2f}"
              f"{(h.unrealized_pnl_pct if h.unrealized_pnl_pct is not None else 0):>+9.1f}"
              f"{(h.weight_pct or 0):>8.1f}")
    if total_cost:
        pnl = (total_mv - total_cost) / total_cost * 100 if total_mv else 0
        print("-" * 89)
        print(f"{'合计':<8}{'':>10}{'':>12}{total_cost:>14.2f}{total_sgd:>14.2f}"
              f"{total_mv:>14.2f}{pnl:>+9.1f}")


def cmd_trades(args):
    cfg = load_config()
    _, conn = get_context(cfg)
    rows = PortfolioTracker(conn).trades()
    if not rows:
        print("暂无交易记录。")
        return
    for t in rows:
        print(f"#{t['id']:<4}{t['traded_at']:<22}{t['symbol']:<8}{t['shares']:>8.4g} 股 "
              f"@ ${t['price_usd']:<9.2f} 汇率 {t['fx_sgd_per_usd']:.4f}  {t['note']}")


def cmd_delete_trade(args):
    cfg = load_config()
    _, conn = get_context(cfg)
    if PortfolioTracker(conn).delete_trade(args.trade_id):
        print(f"已删除交易 #{args.trade_id}")
    else:
        sys.exit(f"未找到交易 #{args.trade_id}")


# ============================================================
def main():
    p = argparse.ArgumentParser(description="巴菲特策略美股定投系统")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scan", help="扫描 S&P 500 并打分（免费档自动断点续扫）")
    sp.add_argument("--tickers", help="只扫描指定股票，逗号分隔，如 AAPL,KO,MSFT")
    sp.add_argument("--limit", type=int, help="只扫描前 N 只")
    sp.add_argument("--force", action="store_true", help="忽略近期扫描记录，强制重扫")
    sp.set_defaults(func=cmd_scan)

    rp = sub.add_parser("report", help="生成本月 HTML 报告和买入建议")
    rp.set_defaults(func=cmd_report)

    bp = sub.add_parser("buy", help="记录一笔实际买入")
    bp.add_argument("symbol")
    bp.add_argument("shares", type=float)
    bp.add_argument("--price", type=float, required=True, help="成交价（USD）")
    bp.add_argument("--fx", type=float, help="汇率：1 USD 兑多少 SGD（不填自动获取）")
    bp.add_argument("--note", help="备注")
    bp.set_defaults(func=cmd_buy)

    pp = sub.add_parser("portfolio", help="查看持仓")
    pp.set_defaults(func=cmd_portfolio)

    tp = sub.add_parser("trades", help="查看全部交易记录")
    tp.set_defaults(func=cmd_trades)

    dp = sub.add_parser("delete-trade", help="删除一笔交易记录")
    dp.add_argument("trade_id", type=int)
    dp.set_defaults(func=cmd_delete_trade)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
