# CLAUDE.md — 项目背景与迭代须知

## 这是什么

巴菲特策略美股定投/建仓系统。用户（个人投资者，新加坡）**每月定投 5000 新币**买美股，
所有金额涉及 SGD↔USD 换算（汇率取 Frankfurter/ECB 免费 API）。
两个入口：CLI（`main.py scan/report/buy/portfolio`）和 Web 页面（`webapp.py`，端口 8600）。

## 架构地图

```
main.py / webapp.py          # CLI 与 Flask Web 两个入口，共用下面所有模块
src/data/yf_client.py        # 默认数据源 yfinance（免费无配额）
src/data/fmp_client.py       # 备用数据源 FMP（接口与 YFClient 完全一致）
src/data/cache.py            # SQLite 缓存：财报90天、报价/汇率12小时
src/strategy/quality.py      # 第一层：8 项巴菲特质量标准 → 质量分
src/strategy/valuation.py    # 第二层：Owner Earnings DCF → 内在价值/安全边际
src/strategy/scoring.py      # 第三层：综合分（质量70%+估值30%），结果落 scores 表
src/portfolio/allocator.py   # 每月定投分配（集中1-3只、25%集中度上限、无候选则持币）
src/portfolio/builder.py     # 一次性建仓：估值回归预期收益模型
src/portfolio/tracker.py     # 持仓账本（trades 表）
src/report/                  # HTML 月报；src/webapp/templates/app.html 是交互页面
data/market_scan.db          # 全部状态：API缓存 + 评分 + 持仓账本（个人数据，不进git）
config.yaml                  # 全部阈值 + FMP key（不进git）；config.example.yaml 是模板
```

策略层不感知数据源：YFClient/FMPClient 返回相同的 FMP 风格字段名。

## 关键决策与踩坑记录（不要重蹈）

1. **数据源默认 yfinance，不是 FMP。** 2025-08 后注册的 FMP key 只能用
   `financialmodelingprep.com/stable` 端点（旧 `/api/v3` 返回 403 Legacy），
   且免费档实测 S&P 500 大多数股票返回 402 premium-only（87/96 被拒）、
   财报 limit≤5 年、外汇/成分股/国债端点全部不开放、被拒请求照样烧 250 次/天配额。
   仅当用户升级 FMP 付费档（10 年历史）才值得把 `data_source` 切回 fmp。
2. **股东盈余必须与 FCF 取较小者**（valuation.py `_owner_earnings`）。
   教训：Netflix 的 D&A 含约 160 亿内容资产摊销，而内容开支走经营现金流不走 capex，
   "净利润+D&A−维持性capex"公式会把股东盈余虚增约 3 倍，曾导致 NFLX 假性 42% 低估。
   任何修改估值公式时先想想内容/无形资产摊销型公司。
3. **银行/保险股天然过不了毛利率筛**（无该科目）——这是特性不是 bug，
   与"看不懂的不买"一致；用户想投的话让他单独分析。
4. S&P 500 成分股来自 GitHub `datasets/s-and-p-500-companies` CSV（免费），
   代码里把 `BRK.B` 格式转成 `BRK-B`。

## 部署（生产环境在 dig 服务器）

- **用户实际使用的是 dig 上的部署**：`dig:/opt/buffett-stock-picker-system`
  （SSH 别名 dig = root@dig.local = 192.168.88.15），systemd 服务 `buffett-webapp`
  （开机自启+自动重启），Web 端口 8600，venv 在项目下 `venv/`。
- **持仓账本以 dig 上的 data/market_scan.db 为准**，本地克隆只是开发副本。
- 更新部署走 git，不要用 rsync（rsync 会覆盖 dig 上的 config.yaml）：
  ```bash
  git push origin main
  ssh dig 'cd /opt/buffett-stock-picker-system && git pull && systemctl restart buffett-webapp'
  ```
- **`config.yaml` 不进 git**，改了阈值/新增配置项必须手动同步到 dig 上那一份，
  否则代码里的 `cfg.get(...)` 会静默回退到旧默认值。
- 改动策略逻辑后，dig 上要单独跑一次重算（走缓存，不烧配额）：
  ```bash
  ssh dig 'cd /opt/buffett-stock-picker-system && venv/bin/python main.py scan --force'
  ```
- 页面无认证，仅限内网；若要公网访问需加认证/反代。

## 开发约定

- 所有策略阈值都在 `config.yaml`，改阈值不改代码；`config.yaml` 含真实 API key，
  永远不进 git（.gitignore 已排除），新环境从 `config.example.yaml` 复制。
- **离线测试方法**：往 SQLite 缓存注入模拟数据源响应即可跑通全流程，
  不需要网络/API key——构造 income/balance/cashflow/quote/profile 的缓存条目
  （key 形如 `yf_statements:SYM`、`quote:SYM`），然后 `scan --tickers SYM`。
- 扫描支持断点续扫：25 天内扫过的自动跳过（`--force` 强制），中断重跑即可。
- 修改估值/质量逻辑后：`python main.py scan --force`（走缓存，约 1-2 分钟）重算全部评分。
- 提交信息用中文；用户是个人项目，保持依赖轻量（requests/pandas/jinja2/PyYAML/yfinance/flask）。

## 远端仓库

git@github.com:bitailab/-buffett-stock-picker-system.git
（仓库名开头的连字符是创建时误输，用户尚未改名；SSH 推送权限，无 gh CLI/API token。）
