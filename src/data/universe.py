"""S&P 500 成分股列表。优先走 FMP 端点（带 30 天缓存），失败时用内置的巴菲特风格核心清单兜底。"""
from .fmp_client import FMPClient, FMPError

# FMP 免费档拿不到成分股端点时的兜底清单：
# 伯克希尔实际持仓 + 典型巴菲特风格大盘股
FALLBACK_TICKERS = [
    "AAPL", "AXP", "BAC", "KO", "CVX", "OXY", "KHC", "MCO", "CB", "DVA",
    "V", "MA", "MSFT", "GOOGL", "JNJ", "PG", "COST", "WMT", "HD", "MCD",
    "PEP", "UNH", "ABBV", "TXN", "LOW", "NKE", "SBUX", "ADP", "ITW", "SHW",
]


def get_universe(client: FMPClient) -> list[str]:
    try:
        rows = client.sp500_constituents()
        symbols = sorted({r["symbol"] for r in rows if r.get("symbol")})
        if symbols:
            return symbols
    except FMPError:
        pass
    return list(FALLBACK_TICKERS)
