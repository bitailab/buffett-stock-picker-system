# CLAUDE.md — 项目背景与迭代须知

## 这是什么

巴菲特策略美股定投/建仓系统。用户（个人投资者，新加坡）**每月定投 5000 新币**买美股，
所有金额涉及 SGD↔USD 换算（汇率取 Frankfurter/ECB 免费 API）。
两个入口：CLI（`main.py scan/report/buy/portfolio`）和 Web 页面（`webapp.py`，端口 8600）。

## 架构地图

```
main.py / webapp.py          # CLI 与 Flask Web 两个入口，共用下面所有模块
src/data/edgar_client.py     # 默认数据源 SEC EDGAR XBRL（免费无配额，10 年年报）
src/data/yf_client.py        # 备用数据源 yfinance；EDGAR 无行情，仍靠它取报价/汇率/国债/成分股
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

策略层不感知数据源：EDGARClient/YFClient/FMPClient 返回相同的 FMP 风格字段名。
EDGARClient 继承 YFClient，只覆盖三张财报表和 profile。

## 关键决策与踩坑记录（不要重蹈）

0. **数据源默认 EDGAR（2026-07 迁移）。** yfinance 年报只有 4 年，且 503 只里
   只有 2 只真有 5 年——其余 358 只是 yfinance 在末尾补了个全 None 的空行，
   凑够 `len(income)==5` 骗过了 `min_years`，另 143 只（含 MSFT/GOOGL/AMZN/V/MA/PG）
   连空行都没有，被 `quality.py` 直接拒绝、连"未达标"都不显示。
   **EDGAR 四个必踩的坑**（见 `edgar_client.py` 文件头，都有实测证据）：
   (a) 标签必须**逐年回退**，不能选中一个用到底——ASC 606 前后营收标签不同，
       Alphabet 2025 财年又换回 `Revenues`；
   (b) EPS/股本要按**该事实的申报日期**做拆股复权，不能按财年末。公司只把最近
       2-3 个可比年度追溯调整：GOOGL 的 FY2021 EPS 在 2022 年报里是 112.20、
       2023 年后的年报里变成 5.61，而 FY2019 永远停在 49.16。按财年末复权会把
       已调整过的年份复权两次（曾让 ACGL 出现 -66.5% 的假回购）。
   (c) 有公司压根不报某科目——Alphabet/FactSet 无 `GrossProfit`（用营收−成本推导），
       Apple 近两年不再单列 `InterestExpense`（并入其他收支净额，导致利息覆盖假阴性）。
   (d) 总债务要**分层组装**：Adobe 只打聚合的 `LongTermDebt`（含当期到期部分），
       Exxon 只打 `LongTermDebtAndCapitalLeaseObligations`，ACGL 只打 `SeniorLongTermNotes`；
       而 `DebtCurrent` 已含长债当期部分，与 `LongTermDebtCurrent` 相加会重复计算。
       **已知缺口**：银行长期债务在 us-gaap 顶层无标签（JPM 只有 ShortTermBorrowings）。
   还有一个 SEC 自身的坑：**ticker→CIK 映射会指向控股重组后的新空壳主体**
   （XOM 被映射到 CIK 2115436，只有 ffd 数据；真实历史在 34088）。
   见 `TICKER_CIK_OVERRIDES`；症状是 companyfacts 返回 200 但没有 us-gaap 命名空间。
1. **FMP 免费档基本不可用。** 2025-08 后注册的 FMP key 只能用
   `financialmodelingprep.com/stable` 端点（旧 `/api/v3` 返回 403 Legacy），
   且免费档实测 S&P 500 大多数股票返回 402 premium-only（87/96 被拒）、
   财报 limit≤5 年、外汇/成分股/国债端点全部不开放、被拒请求照样烧 250 次/天配额。
   现在 EDGAR 免费就给 10 年，**没有任何理由再切回 fmp**（付费档也没有）。
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

git@github.com:bitailab/buffett-stock-picker-system.git
（2026-07 已改名去掉开头误输的连字符；旧地址 `-buffett-...` 目前仍能靠 GitHub
重定向工作，但别再用。SSH 推送权限，无 gh CLI/API token。dig 上也有 deploy key。）
