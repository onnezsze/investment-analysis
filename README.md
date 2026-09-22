# Investment Analysis 技能

**丢一个代码就跑的投资分析工作流**：股票用财报/估值，加密用资金费率/杠杆/场所 —— 双轨，跨 Hermes / Claude / GPT 三端通用。

## 两条轨道

### A. 股票（`references/fundamentals.md` · `references/technical.md`）

- **基本面全流程**：分析师团队情报 4 份报告 → 投研团队多空辩论 → 交易主管执行计划 → 首席投资官审批。
- **技术面深度分析**：风险前置免责声明 → 量化数据仪表盘 → 多维时间框架共振 → 相对强度与波动率 → 交易剧本与风险矩阵 → 最终建议。
- **数据引擎**：`scripts/equity_snapshot.py`（yfinance）——一条命令取全三年财报、估值倍数、同业对标、日线/周线/4小时三周期指标、ATR/布林带宽分位、量价分布、斐波那契、相对强弱、分析师与机构持仓。

### B. 加密货币永续合约（`references/crypto.md` · `prompts/crypto-prompt.md`）

面向 **BTCUSDT / SOLUSDT 等 USDT 本位永续**的交易备忘录。结构由 TypeSafe（jev-1.13.0）对"股票工作流能否用于加密"的结构化判决驱动：

| 判断 | 结果 | 改造 |
|---|---|---|
| 整体可迁移性 | Score 1.50 | 只复用技术面+辩论结构，**数据层整体替换** |
| 基本面锚缺失 | Score 2.86（严重） | 删除估值/护城河/管理层，改为资金费率+OI+代币经济；**禁止编造财报类结论** |
| 周期错配 | Noul 0.80 | 主决策周期改 **4H**（1H 执行 / 日线看结构） |
| 衍生品盲区 | Noul 0.94 | 强制纳入资金费率、OI、多空比、主动买卖比、OI 背离 |
| 场所风险缺口 | Noul 0.95 | 量化价差/深度/滑点/跨场所资金费率差 + 场所风险清单 |
| 仓位模型 | Choice 0.98 | **波动率目标仓位 + 杠杆上限 + 爆仓距离校验** |
| 清算数据 | Noul 0.74 | 清算聚集代理价位，止损避开清算簇 |
| 链上必要性 | Noul 0.68 | 解锁悬顶/市值占FDV/稳定币净发行；不可核验时降级 |
| 最值得保留 | Choice 0.73 | **数字审计质量门原样保留** |

- **数据引擎**：`scripts/crypto_snapshot.py` —— **数据源只有两类**：
  - **交易所公开 API**（Binance + HTX）：行情/K线/资金费率及历史/OI 及历史/多空账户比/大户持仓比/主动买卖比/订单簿深度与滑点多档模拟/分时段与工作日流动性
  - **dogdoing.ai** 公开 JSON 接口：社交热度榜、AI 情绪与摘要、OI 背离、链上代币信息（市值/FDV/持有人/Top10 集中度/流动性）、合约审计、KOL 观点、预测市场、Alpha 热点、涨跌幅榜、资讯、恐惧贪婪指数
  - 输出**波动率目标仓位表**（名义头寸/隐含杠杆/交易所杠杆/保证金/估强平价/爆仓距离校验），并区分「隐含杠杆」与「交易所杠杆」
- 已移除 CoinGecko 与 alternative.me；**无全网市值/BTC 占比/稳定币数据源**，报告中不得出现此类结论
### C. 标的发现与 tradfi（Traditionally Finance）

- **标的发现** `scripts/crypto_scan.py`：从 dogdoing 挖机会（社交热度 / OI 背离 / 涨跌幅榜 / Alpha 热点 / 美股叙事），交叉校验 Binance 与 HTX 的可交易性，按资产类别分别打分，输出可交易榜单。
- **tradfi 覆盖**：Binance `contractType=TRADIFI_PERPETUAL` 共 **199 个** —— 股票（AAPL/NVDA/TSLA/AMD/MU/MSTR…163 个）、港股 15、韩股 8、商品（XAU 黄金/XAG 白银/CL 原油）8、外汇 1、Pre-IPO 2（含 Anthropic）；ETF 如 TQQQ/QQQ/SPY/SOXL/DRAM/EWT/IWM/URNM。**用同一套 Binance 凭证即可交易**，且 **24/7 无休市**（实测 1H K线零缺口）。
- **tradfi 口径差异**：基准指数从 BTC 改为 SPYUSDT；无现货K线（走永续）；资金费率常显著非零（实测 AMD 年化 85.8%）。
- **执行层** `scripts/binance_exec.py`（默认 dry-run，`--live` 才发单）+ `scripts/autotrade.py`（4 小时巡检；主开关文件 `~/.binance_futures_autotrade.on` 存在才真下单，删掉即停）。

- **决策层** `scripts/crypto_decide.py`：借鉴 [jarrodwatts/jev-trader](https://github.com/jarrodwatts/jev-trader) 的 TypeSafe 范式 —— 代码构建相对化状态 → 一次请求并行四问 → **置信度门控** → 代码侧硬约束（成本/RR≥2/爆仓距离）→ 写入 `decisions.jsonl` 账本；`--resolve` 回填结果并按「动作 × 置信度桶」统计命中率。详见 `references/jev-patterns.md`
- **研究版**（`references/crypto-research.md`）：长期代币研究框架（Tokenomics / NVT / TVL / 活跃地址 / 解锁日历），链上数据多需付费接口，取不到必须标注"未核验"。

## 质量门（两轨共用）

`scripts/audit_memo.py` —— 把备忘录里每个数字回头对账数据快照，输出「直接命中 / 推导命中 / **待确认来源**」三组。待确认的每个数字都必须给出出处（官方财报 / 新闻链接 / 计算过程），给不出即视为编造。实测：NVDA 备忘录 314 个数字 100% 可回溯；BTCUSDT 备忘录 194 个数字 100% 可回溯。

## 目录结构

```
investment-analysis/
├── SKILL.md                       # Hermes 技能说明（触发条件/流程/坑/自查）
├── README.md                      # 本说明
├── scripts/
│   ├── equity_snapshot.py         # 股票数据引擎（yfinance）
│   ├── crypto_scan.py             # 标的发现器（dogdoing 挖机会 → 可交易榜单，含 tradfi）
│   ├── crypto_snapshot.py         # 数据引擎（加密 + tradfi 永续，含资金费率/OI/深度/滑点）
│   ├── crypto_decide.py           # 单次决策引擎（TypeSafe 单点决策 + 置信度门控 + 账本）
│   ├── binance_exec.py            # 执行层（USDT-M 下单，默认 dry-run，需 --live）
│   ├── autotrade.py               # 定时巡检编排（扫描→决策→门控→执行→记账，含硬安全阀）
│   └── audit_memo.py              # 数字审计质量门
├── references/
│   ├── fundamentals.md            # 股票基本面模板
│   ├── technical.md               # 股票技术面模板
│   ├── crypto.md                  # 加密交易模板（TypeSafe 判决驱动）
│   ├── crypto-research.md         # 加密研究版模板（保留）
│   └── jev-patterns.md            # 借鉴自 jev-trader 的 TypeSafe 决策范式
└── prompts/
    ├── fundamentals-prompt.md     # 纯 Prompt 正文（复制即用）
    ├── technical-prompt.md        # 纯 Prompt 正文（复制即用）
    ├── crypto-prompt.md           # 加密交易版纯 Prompt（含自检清单）
    └── crypto-research-prompt.md  # 加密研究版纯 Prompt（保留）
```

## 用法

### 命令行（Hermes Agent / 任意终端）

```bash
# 股票
python3 scripts/equity_snapshot.py NVDA
python3 scripts/equity_snapshot.py 600519.SS --peers 000858.SZ,000568.SZ,600809.SH,002304.SZ
python3 scripts/equity_snapshot.py 0700.HK

# 加密永续
python3 scripts/crypto_snapshot.py BTCUSDT --equity 10000 --leverage 5
python3 scripts/crypto_snapshot.py WIFUSDT --equity 5000 --venue both

# 数字审计
python3 scripts/audit_memo.py memo_NVDA_20260922.md snapshot_NVDA_20260922.json --extra "..."
```

依赖：`yfinance pandas numpy`（股票）；加密引擎只依赖 `pandas numpy` + 标准库。

### Claude / GPT（或任何大模型）

把 `prompts/` 下对应文件的**全文**粘贴进对话，替换 `{...}` 占位符即可。四个文件首行即 Prompt 正文，无 Markdown 包装，复制即用。

### Hermes Agent

直接说"分析 NVDA"或"分析 BTCUSDT 永续，权益 1 万"，技能会自动触发并走对应轨道。

## 免责声明

本项目所有输出均为基于公开数据的概率性推演，**不构成任何投资建议**。加密资产（尤其杠杆衍生品）存在本金全损风险。请自行完成独立尽调。
