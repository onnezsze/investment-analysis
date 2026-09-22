---
name: investment-analysis
description: "丢一个股票代码就跑的投资分析工作流：真实数据落地 + 机构级投研备忘录。"
version: 2.0.0
author: Lucas (htxlucas), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, investment, stock, analysis, workflow, yfinance, equity-research]
---

# 投资分析工作流 (Investment Analysis Workflow)

**输入一个股票代码，输出一份机构级投资备忘录 (Investment Memo)。**

本技能把两套 Prompt 模板（基本面全流程 / 技术面深度）做成**可执行管线**：取数由脚本完成（真实数据，非模型记忆），角色分析由 agent 完成。脚本负责算数，agent 负责推理和下结论。

- 数据引擎：`scripts/equity_snapshot.py`（yfinance，覆盖美股 / 港股 / A股 / 加密）
- 模板原文：`references/fundamentals.md`（基本面 4 部分）、`references/technical.md`（技术面 6 章）、`references/crypto.md`（加密）
- 跨端纯 Prompt：`prompts/*.md`（复制粘贴给任何大模型用，不带工具）

> 本技能不构成投资建议。所有报告开头必须带免责声明（`references/technical.md` 内有原文）。

## When to Use（触发条件）

- 用户给了股票代码/名称，要一份**机构级报告**："分析一下 XX / 帮我看看 XX / 值不值得买 / 出一份投资备忘录"。
- 用户点名要用 **基本面** 或 **技术面** 模板，或说"用投资分析模板跑一遍"。
- 用户要 **多空辩论 / 投委会结论 / 交易计划**（入场·止损·止盈）。
- 加密资产（BTC/ETH/SOL…）要机构级报告 → 同一管线 + `references/crypto.md` 的角色结构。

**Don't use for:** 只查当前价格/涨跌幅；单一指标速算；不需要结构化报告的一次性问答。深度个股管线（22 维 + 66 评委 + DCF/Comps 建模 + HTML 报告）走 `deep-analysis` 技能。

## Prerequisites（前置条件）

- 必须拿到 **股票代码**（缺则先问，不要猜）。市场可推断。
- 依赖：`yfinance`、`pandas`、`numpy`（本机已装；缺失时 `pip install yfinance pandas numpy`）。
- 输出目录默认 `~/investment_snapshots/`（`--out` 可改）。

## How to Run（标准工作流 · 6 步）

### Step 1 · 一条命令取全量真实数据

```bash
python3 ~/.hermes/skills/finance/investment-analysis/scripts/equity_snapshot.py <TICKER> \
        [--peers 000858.SZ,000568.SZ,600809.SH,002304.SZ]   # 非美股权重建议显式指定同业
        [--no-peers]                                        # 只要行情/基本面，不要同业
```

代码格式：美股 `NVDA`；港股 `0700.HK`；A股 `600519.SS` / `000858.SZ`；加密 `BTC-USD`。

产出两份文件（路径会打印在 stdout）：
- `snapshot_<TICKER>_<YYYYMMDD>.json` — 完整结构化数据
- `dashboard_<TICKER>_<YYYYMMDD>.md` — 「关键数据仪表盘」，可直接粘进报告

**一次抓到的内容**：报价/52周高低/均量 · PE·ForwardPE·PB·PS·EV/EBITDA·股息率·FCF收益率 · 三年财报（收入/毛利/净利/OCF/Capex/FCF/ROE + 同比）· 财务健康（流动比/速动比/债务股本/净债）· ROIC 近似 · 日线·周线·4小时 三周期（MA20/50/200、RSI14、MACD、布林带宽度+1年分位、ATR14）· 枢轴点支撑阻力聚类 · 成交量分布（VPOC / 高量节点 / 70% 价值区）· 斐波那契回撤与扩展 · 相对强弱 vs 本市场基准（1M/3M/6M/1Y + 上行/下行捕获率）· 分析师评级·目标价·90天评级变动 · 空头兴趣 · 机构持仓 Top10 · 同业估值对标表

### Step 2 · 核对数据缺口（不许编，不许填 0）

读 JSON 的 `data_quality.missing_or_degraded`：

- 非空 → 逐条用 `web_search` 补（搜索式见 Step 3），补到就更新报告；补不到就**显式写"数据缺失"**，绝不用 0 或推测值顶替。
- 同业行的 `data_flags`（如"PS异常低(疑似币种/口径不一致)"、"估值数据缺失"）→ 该行不得参与中位数计算，或注明不可比。

### Step 3 · 抓脚本拿不到的情报（新闻 / 宏观 / 舆情 / 员工口碑）

脚本覆盖行情与财务；以下必须 agent 用 `web_search` / `web_extract` / `browser_exec` 抓，**每条结论带来源**：

| 模块 | 搜索式 | 用途 |
|---|---|---|
| 公司新闻 | `"{公司名} {代码} 最新公告 财报 合同 2026"` | 新闻事件驱动分析、量化影响 |
| 产业链 | `"{公司名} 上游供应商 下游客户 原材料价格"` | 产业链 + 波特五力 |
| 行业 | `"{行业} 行业规模 增速 TAM 2026"` | 行业动态、竞争强度 |
| 宏观政策 | `"{行业} 监管政策 反垄断 2026"` / `"{国家} 利率 通胀 汇率"` | 宏观与监管雷达 |
| 管理层 | `"{公司名} CEO 高管变动 员工 评价"` | 管理层评估、关键人物风险 |
| 员工口碑 | `"{公司名} Glassdoor OR 脉脉 OR Blind 评分"` | 组织文化、人才净流入/流出 |
| 社交舆情 | `site:xueqiu.com {代码}` / `"{公司名} 舆情 讨论"` | 情绪倾向、热度 |
| 分析师分歧 | `"{公司名} 研报 目标价 上调 下调"` | 与脚本的 rating_actions_90d 交叉验证 |

### Step 4 · 按模板角色写报告

**先读模板**：基本面 `references/fundamentals.md`（Part 1 四份分角色情报 → Part 2 多空激辩 → Part 3 交易主管执行计划 → Part 4 CIO 审批）；技术面 `references/technical.md`（免责声明 → 论点与置信度 → 仪表盘 → 三周期共振 → 相对强弱与波动率 → 交易剧本与风险矩阵 → 结论）。

铁律：
1. 模板里所有 `$XXX.XX` / `X%` 占位符，**必须用 snapshot 的真实数字替换**。
2. 技术面报告的仪表盘直接引用 `dashboard_<TICKER>_<date>.md`。
3. 三周期趋势表述必须与 snapshot 的 `technicals.weekly/daily/h4.trend` 一致（脚本已给出客观排列，agent 做解读而非改写事实）。
4. 多空双方必须**互相反驳对方的核心论据**，不是各说各话。
5. 每条量化论断标注来源（`Yahoo Finance` / 新闻链接）。
6. 报告开头保留免责声明原文。

### Step 5 · 质量门（不过不发）

| 检查项 | 标准 |
|---|---|
| 数字可回溯 | 每个百分比/价格都能在 snapshot JSON 或抓取的链接里找到 |
| 风险回报比 | 用 `terminal` 实算 `(TP1-Entry)/(Entry-SL)`，禁止心算；<2:1 必须说明为何仍可接受或判驳回 |
| 止损与 ATR | 止损位与 `technicals.atr_position_sizing`（2×ATR / 3×ATR）或结构位之一有推导关系 |
| 目标位 | 至少两个，且锚定真实价位（前高/形态测量/斐波那契扩展） |
| 缺口标注 | `missing_or_degraded` 中未能补上的字段在报告里明确标注 |
| 币种一致 | 港股 HKD、A股 CNY、美股 USD；跨国同业比较需说明币种与汇率口径 |
| 篇幅 | 达标即止，不灌水；每个章节必须有结论句，不写"值得关注"这类空话 |

### Step 6 · 交付

备忘录写到 `~/investment_snapshots/memo_<TICKER>_<YYYYMMDD>.md`，结构 = 模板原结构。交付时给：
- 一行结论（动作 + 关键价位 + 风险回报比）
- 备忘录文件路径（Telegram 用 `MEDIA:<绝对路径>` 直接发文件）
- 数据时间戳 + 缺口清单

## Quick Reference

```bash
# 美股（同业自动发现：同市场推荐 + 行业 Top）
python3 .../equity_snapshot.py NVDA
# 港股
python3 .../equity_snapshot.py 0700.HK
# A股（建议显式指定同业，Yahoo 自动发现的白酒同业会串到跨市场）
python3 .../equity_snapshot.py 600519.SS --peers 000858.SZ,000568.SZ,600809.SH,002304.SZ
# 加密
python3 .../equity_snapshot.py BTC-USD
```

JSON 关键路径：`meta`（名称/市场/行业/业务简介）· `quote` · `valuation` · `financials_3y` · `health_and_growth` · `roic_approx` · `technicals.{weekly,daily,h4,levels,volume_profile,fibonacci,atr_position_sizing,bb_width_percentile_1y}` · `relative_strength` · `sentiment`（分析师/空头/机构持仓）· `peers` · `data_quality`。

## Pitfalls（实测踩过的坑）

- **A股基准**：Yahoo 的 `000300.SS` 只有 1 根 K 线 → 脚本已改用 `510300.SS`（沪深300ETF），相对强弱才可用。
- **同业串市场**：Yahoo 行业 Top 公司以美股为主，非美股会串（茅台曾被对成沃尔玛/可口可乐）。脚本改为「同市场推荐 + 行业Top」双源合并，并在每行标 `source`；`跨市场·慎用` 的行需人工取舍，宁可用 `--peers` 指定本市场同业。
- **尾部 NaN K线**：yfinance 日线最后一根常为 NaN → 脚本已 dropna，勿手工取 `iloc[-1]` 判断价格。
- **行业 slug 404**：`yf.Industry("Consumer Electronics")` 会 404，必须小写连字符（`consumer-electronics`）；脚本已容错并回退。
- **港股/A股财报口径**：TENCENT 收入含投资收益等，净利率与 A股白酒不可直接横比；跨市场对比必须说明口径。
- **`info` 偶发限流**：返回缺字段时重跑一次即可；若 `pe_ttm` 为空说明该字段没取到，不要写 0。
- **分析师目标价币种**：港股目标价是 HKD、A股是 CNY，别按 USD 读。
- **不要用模型记忆里的价格**：一切以 snapshot 的 `meta.as_of` 时间戳为准；用户问"最新"时先看时间戳。
- **免责声明**：技术面模板要求开篇粘贴原文，不可省略、不可缩短。

## Verification（交付前自查）

- [ ] `equity_snapshot.py` 成功跑出 JSON + dashboard（失败先修数据，不进入写报告）
- [ ] `data_quality.missing_or_degraded` 已逐条处理（补齐或显式标注）
- [ ] 新闻/舆情部分有真实链接，且与 `sentiment.rating_actions_90d` 无矛盾
- [ ] 基本面报告含：护城河判定、三年比率、现金流与净利匹配度、同业估值表、增长/风险矩阵、管理层评分(1-10)
- [ ] 多空双方互相反驳了对方的**核心论据**
- [ ] 交易计划含：动作、仓位%、入场区、止损、TP1/TP2、风险回报比（实算）
- [ ] CIO 章节给出批准/驳回的明确结论
- [ ] 技术面报告以免责声明开头，仪表盘数字与 snapshot 完全一致
- [ ] 备忘录文件已落盘且路径已交付给用户
