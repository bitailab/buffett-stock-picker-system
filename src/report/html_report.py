"""用 jinja2 渲染月度 HTML 报告。"""
import datetime as dt
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = Path(__file__).parent / "templates"


def render_report(plan, ranking: list[dict], holdings, reports_dir: str,
                  top_n: int = 30) -> str:
    """plan: AllocationPlan；ranking: 按分数排序的 StockScore dict；holdings: list[Holding]。
    返回生成的报告文件路径。"""
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    tpl = env.get_template("report.html")
    now = dt.datetime.now()
    html = tpl.render(
        month=now.strftime("%Y-%m"),
        generated_at=now.strftime("%Y-%m-%d %H:%M"),
        plan=plan,
        ranking=ranking[:top_n],
        holdings=holdings,
    )
    out_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"report_{now.strftime('%Y-%m')}.html"
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)
