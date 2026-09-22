---
name: investment-analysis
description: "丢一个代码就跑的投资分析工作流：股票(财报/估值) + 加密永续(资金费率/杠杆/场所) 双轨。"
version: 3.0.0
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
- **加密货币永续合约交易**（BTC/ETH/SOL/长尾币，HTX/Binance）：要交易计划、杠杆与仓位、资金费率与拥挤度判断、入场/止损/爆仓距离 → 走下方「加密货币交易工作流」（数据引擎 `scripts/crypto_snapshot.py`，模板 `references/crypto.md`）。
- 加密资产的**长期研究**（通证经济/NVT/TVL/解锁日历）→ `references/crypto-research.md`（研究版框架，链上数据多需付费接口，取不到必须标注"未核验"）。

**Don't use for:** 只查当前价格/涨跌幅；单一指标速算；不需要结构化报告的一次性问答。深度个股管线（22 维 + 66 评委 + DCF/Comps 建模 + HTML 报告）走 `deep-analysis` 技能。

## Prerequisites（前置条件）

- 必须拿到 **股票代码**（缺则先问，不要猜）。市场可推断。
- 依赖：`yfinance`、`pandas`、`numpy`（本机已装；缺失时 `pip install yfinance pandas numpy`）。
- 输出目录默认 `~/investment_snapshots/`（`--out` 可改）。

## How to Run · A. 股票工作流（6 步）

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

**数字审计（必跑，机器对账）**：

```bash
python3 ~/.hermes/skills/finance/investment-analysis/scripts/audit_memo.py \
        ~/investment_snapshots/memo_<TICKER>_<date>.md \
        ~/investment_snapshots/snapshot_<TICKER>_<date>.json \
        --extra "962.21,89.5,47.4"      # 已知来自官方财报/新闻/推算的数字，逗号分隔
```

输出三组：① 直接命中快照 ② 命中推导值/白名单 ③ **待确认来源**。
**第③组的每个数字都必须逐条给出出处**（官方财报链接 / 新闻链接 / 计算过程）；给不出出处的就是编造，必须删除或改正后重跑，直到"待确认"只剩能解释的项。重跑取数脚本后必须重新审计（防陈旧数字）。

### Step 6 · 交付

备忘录写到 `~/investment_snapshots/memo_<TICKER>_<YYYYMMDD>.md`，结构 = 模板原结构。交付时给：
- 一行结论（动作 + 关键价位 + 风险回报比）
- 备忘录文件路径（Telegram 用 `MEDIA:<绝对路径>` 直接发文件）
- 数据时间戳 + 缺口清单

## How to Run · B. 加密货币交易工作流（TypeSafe 判决驱动）

本分支的**改造依据**来自 TypeSafe（jev-1.13.0）对"股票工作流能否用于加密"的结构化判断：

| 判断 | 结果 | 改造动作 |
|---|---|---|
| 整体可迁移性 | Score 1.50 | 只复用技术面+辩论结构，**数据层整体替换**为 `crypto_snapshot.py` |
| 基本面锚缺失 | Score 2.86（严重 0.87） | 删除估值/护城河/管理层章节，改为**资金费率+OI+代币经济**；禁止编造财报类结论 |
| 周期错配 | Noul 0.80 | 主决策周期改 **4H**（1H 执行 / 日线看结构） |
| 衍生品盲区 | Noul 0.94 | 强制纳入资金费率、OI、多空比、主动买卖比、OI 背离 |
| 场所风险缺口 | Noul 0.95 | 量化价差/深度/$10万滑点/跨场所资金费率差 + 场所风险清单 |
| 仓位模型 | Choice 0.98 | **波动率目标仓位 + 杠杆上限 + 爆仓距离校验**（替代固定仓位比例） |
| 清算数据 | Noul 0.74 | 输出清算聚集代理价位，止损需避开清算簇 |
| 链上必要性 | Noul 0.68 | 输出解锁悬顶/市值占FDV/稳定币净发行；非主流币不可核验时降级为"仅技术面有效" |
| 最值得保留 | Choice 0.73 | **数字审计质量门原样保留** |

**标的发现（先做这一步）**：`scripts/crypto_scan.py` 从 dogdoing 挖机会并校验可交易性：
```bash
python3 .../crypto_scan.py --top 24                # 加密 + tradfi 混合榜
python3 .../crypto_scan.py --only tradfi           # 只看股票/ETF/商品/外汇
```
- 候选来源：`square-hype`(社交热度) / `oi-divergence`(持仓异动) / `gainers` / `losers` / `hotspots`(Alpha) / `us-stocks`(tradfi 叙事)
- 可交易性校验：Binance `PERPETUAL`(加密) 与 `TRADIFI_PERPETUAL`(股票/ETF/商品) + HTX 永续；自动处理 1000/1000000 乘数
- 机会分口径**按资产类别分开**：加密=热度+OI背离+摆幅+费率偏离+流动性；**tradfi=流动性+摆幅+新闻流+费率偏离**（tradfi 无社交热度与 OI 背离）
- 输出 `~/crypto_snapshots/watchlist_<date>.json`，供 `autotrade.py --scan` 直接消费

**步骤**：

1. **取数（一条命令）**：
   ```bash
   python3 ~/.hermes/skills/finance/investment-analysis/scripts/crypto_snapshot.py <SYMBOL>            --equity 10000 [--venue both|binance|htx] [--leverage 5] [--chain-id 56 --contract 0x...] [--no-dogdoing]
   ```
   例：`crypto_snapshot.py BTCUSDT --equity 10000` / `crypto_snapshot.py WIFUSDT --equity 5000`
   产出 `snapshot_<SYM>_<date>.json` + `dashboard_<SYM>_<date>.md`（默认 `~/crypto_snapshots/`）。

   一次抓到：标记价/现货价与基差 · 4H/1H/1D 三周期（EMA/RSI/MACD/ATR%/BB带宽分位）· 已实现波动（√365 年化）与波动率年内分位 · 支撑阻力聚类 · **清算聚集代理** · 资金费率（现值/年化/200期历史正向占比/当前分位/跨场所差/持有成本）· **OI（现值/24h/30日分位/价-量-OI四象限/背离度）** · 多空账户比与大户持仓比 · 主动买卖比 · **订单簿深度/价差/多档滑点模拟** · 分时段与工作日流动性分布 · 相对 BTC 强弱与相关性/Beta · 恐惧贪婪指数（双源）· 市值/FDV/解锁悬顶 · 稳定币净发行代理 · **波动率目标仓位表（含强平价与爆仓距离校验）**。

2. **核对缺口**：读 `data_quality`。链上数据（交易所余额/巨鲸/解锁日历）与清算明细**免费接口拿不到**，必须显式标注"未核验/代理指标"，不得用推测值顶替。

3. **补充情报**：`aggregator` 字段已含 dogdoing.ai 的社交热度、OI 背离、资讯；需要更深的项目/监管信息时再用 `web_search`，每条带链接。

4. **写报告**：按 `references/crypto.md` 的 8 章结构（场所档案 → 市场结构 → 多周期技术 → 资金面与仓位 → 代币经济 → 多空激辩 → 交易计划 → 风控官审批 → 一页结论）。**必须包含**：杠杆+保证金+止损+估强平价、`强平距离≥1.5×止损距离` 校验、资金费率持有成本、滑点约束下的名义上限、2–3 条可观测失效条件。

4.5 **决策层（单次可证伪决策，借鉴 jev-trader 的 TypeSafe 范式）**：备忘录给的是叙事与计划；若要**可统计、可回填**的单次决策，用：
   ```bash
   python3 ~/.hermes/skills/finance/investment-analysis/scripts/crypto_decide.py BTCUSDT \
           --profile swing --equity 10000 --leverage 5      # 真实 TypeSafe 决策（一次请求问 4 个原子问题）
   python3 .../crypto_decide.py BTCUSDT --model mock        # 启发式替身，仅用于管线自测（不可作交易依据）
   python3 .../crypto_decide.py --resolve                   # 回填历史决策结果，按「动作×置信度桶」统计命中率
   ```
   - **代码构建相对化状态**（bps 收益/盘口失衡/深度分档/CVD/资金费率与 OI 拥挤度/成本），模型只做判断
   - 一次请求并行四问：`direction`(Choice，criteria 写明往返成本阈值) + `crowding`(Score) + `executability`(Score) + `invalidation_clarity`(Noul)；代码归一化加权成 conviction（`swing`/`scalp` 两套权重）
   - **置信度门控（2026-09-22 实盘后上调）**：`conf < 0.65`(swing)/`0.70`(scalp) → 观望；`0.65~0.85` → 半仓；`≥0.85` → 标准仓
   - **方向概率下限**：`prob[所选方向] < 0.55`(swing)/`0.58`(scalp) → 观望（"证据不足"，防低置信度硬做）
   - **风险回报比**：`RR < 2.5` → 观望（原 2.0 上调）
   - **建仓三重闸（autotrade）**：① 并发仓位上限 **2** 个；② 与现有持仓 4H 收益率相关性 **>0.70** 视为"同一押注"不再下注（教训：实盘中在 MU 与 MUU（同源标的）各开一笔 → 敞口翻倍）；③ 单标的最多一笔
   - **持仓管理（论点复核，每轮巡检先跑）**：对每个持仓重新问 TypeSafe → ① 模型反向且置信度 ≥0.65 → **平仓**；② 持仓方向概率 <0.40 或置信度 <0.30（论点消失）→ **平仓**；③ 概率 <0.52 或置信度 <0.45（论点弱化）→ 已盈利则**收紧止损**至保本/更优，未盈利则标记，**连续两次弱化即平仓**。平仓前必须先撤销 algo 保护单（`cancel_all_protective`），避免残留条件单
   - **代码侧硬门控**：成本 > 典型波幅 1/3 → 观望；盘口深度不足 → 观望；**RR < 2 或爆仓距离不达标 → 观望**
   - 账本 `~/crypto_snapshots/decisions.jsonl`（含 `executed` 标记，被否决的决策不计入胜率）
   - **注意**：同一状态复跑，模型动作可能在 long/short 间翻转且置信度偏低 → **这正是门控存在的理由**；mock 替身的方向判断可与真实模型完全相反，仅供管线测试

5. **数字审计（必跑）**：
   ```bash
   python3 ~/.hermes/skills/finance/investment-analysis/scripts/audit_memo.py \
           ~/crypto_snapshots/memo_<SYM>_<date>.md ~/crypto_snapshots/snapshot_<SYM>_<date>.json \
           --extra "填快照外的官方/新闻数字"
   ```

6. **交付**：`~/crypto_snapshots/memo_<SYM>_<date>.md` + 仪表盘 + 缺口清单。

7. **执行层（真实下单，默认 dry-run）**：
   ```bash
   python3 .../binance_exec.py account|specs BTCUSDT|order SYM buy --notional 40 --sl .. --tp .. [--live]
   python3 .../autotrade.py --scan 8 --include-tradfi [--dry]     # 定时巡检编排（主开关控制是否真下单）
   ```
   - `binance_exec.py`：**默认只预览，必须显式 `--live` 才发送**；自动按 stepSize/tickSize 取整、校验 MIN_NOTIONAL 与可用保证金；止损止盈用 `reduceOnly` 条件单
   - `autotrade.py`：主开关文件 `~/.binance_futures_autotrade.on` 存在才真下单（**删掉即停**）；硬安全阀=单标的一笔、权益地板（默认 85 USDT）、只做扫描榜内标的、每笔风险 1%
   - ⚠️ **验收纪律**：`crypto_decide.py`、`crypto_scan.py`、`equity_snapshot.py` 我改完必须跑一次真数据自测；`binance_exec.py` 改完必须跑 `order ... `（不带 --live）确认取整与最小名义校验生效

### 数据源分工（重要）

**数据源只有两类（用户指定，不得引入第三类）**：交易所公开 API + dogdoing.ai。缺数据就写"未取到/未核验"，**不要为了补字段去接其他来源**。

| 来源 | 覆盖 | 说明 |
|---|---|---|
| Binance 公开 API（加密 + tradfi） | 行情/K线/资金费率+历史/OI+历史/多空账户比/大户持仓比/主动买卖比/订单簿深度 | 价格的**唯一权威源**。**同时覆盖 tradfi**：`contractType=TRADIFI_PERPETUAL` 共 **199 个**（EQUITY 163 / HK_EQUITY 15 / KR_EQUITY 8 / COMMODITY 8 / FX 1 / PREMARKET 2），如 `XAUUSDT`(黄金)、`TQQQUSDT`/`QQQUSDT`/`SPYUSDT`/`SOXLUSDT`(ETF)、`NVDAUSDT`/`TSLAUSDT`/`MSTRUSDT`(股票)、`CLUSDT`(原油) |
| HTX 公开 API | 资金费率+历史/OI/深度/行情 | 用户所在场所；跨场所资金费率差为独立信号（脚本已标注"历史均值口径"）。HTX 亦有 tradfi 合约（部分与 Binance 重叠），本工作流默认优先 Binance 执行 |
| **dogdoing.ai** 公开 JSON 接口 | 社交热度、AI 情绪与摘要、OI 背离、链上代币信息、合约审计、KOL 观点、预测市场、Alpha 热点、涨跌幅榜、资讯、恐惧贪婪 | `square-hype` `sentiment` `oi-divergence` `token-info` `token-audit` `serenity-tweets` `prediction-markets` `hotspots` `gainers` `losers` `news` `fear-greed` `market-tickers` `klines`；**其价格仅作交叉校验，不作权威价** |
| 交易所 API（后续执行） | 下单/持仓/保证金 | 用户已明确后续执行走交易所 API |
| **TypeSafe（jev-latest）** | 单次决策的语义判断（方向/拥挤度/可执行性/失效清晰度） | 决策层用；`TYPESAFE_API_KEY` 已在环境中；与数据源无关，不引入行情数据 |

**已移除**：CoinGecko（全网市值/FDV/全市场/BTC占比/稳定币）与 alternative.me（情绪指数）—— 用户明令数据源只用上述两类；对应字段在报告中改为"无数据源，未核验"，**禁止用其他来源补**。

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
- **yfinance 字段口径陷阱（实测踩到，脚本已修）**：
  - `dividendYield` 不同版本口径不同（1.7 返回的 0.45 已是百分数）→ 一律用 `dividendRate / 价格` 现算，脚本已改。
  - `info.freeCashflow` 与 `operatingCashflow` 口径不一致（存在明显低报）→ 用 `OCF(TTM) − 最近财年Capex`，脚本已改，字段名 `fcf_ttm_ocf_minus_capex`。
  - 同业倍数会被币种污染（美股 ADR 的 PS、EU 公司的 EV/EBITDA）→ 脚本按"PS 与净利率矛盾""EV/EBITDA > 200"自动打 flag 并剔除，估值的同业中位数只能用 `peers.summary.clean_sample` 内的公司。
- **分析师目标价币种**：港股目标价是 HKD、A股是 CNY，别按 USD 读。
- **数据会随重跑微调**：写报告后若重跑脚本，必须重跑 Step 5 的数字审计（否则会出现"报告写 208.6%、快照实际 208.5%"这类陈旧数字）。
- **不要用模型记忆里的价格**：一切以 snapshot 的 `meta.as_of` 时间戳为准；用户问"最新"时先看时间戳。
- **免责声明**：技术面模板要求开篇粘贴原文，不可省略、不可缩短。

### 加密货币特有的坑（实测）

- **资金费率符号读反**：正费率=**多头付钱给空头**（多头拥挤），负费率=空头付钱给多头。年化 = 8h费率 × 3 × 365。读反会得出完全相反的资金面结论。
- **年化口径**：加密 7×24 用 **√365**，股票用 √252。混用会低估波动率约 20%。
- **24/7 无收盘**：不存在"隔夜缺口回补"；止损必须用条件单/限价单，市价止损在低流动性时段会被滑点打穿。
- **杠杆与强平**：`强平距离 ≈ 1/杠杆 − 维持保证金率 − 手续费缓冲`；必须满足 `强平距离 ≥ 1.5×止损距离`。**杠杆 ≤1x 时不存在强平，1/杠杆 公式失效**（脚本已特判）。
- **滑点就是成本**：BTC 的 $10万滑点≈0 bps，但长尾币（如 WIF）可达 25 bps 以上，叠加双向手续费会吞掉大部分短线收益；名义头寸必须受滑点约束。
- **禁止套用股票口径**：加密没有财报/分析师目标价/净资产。写"市值/FDV"可以，写"PE/护城河/管理层评分"就是编造（TypeSafe 给这条判了 0.87 的"严重"）。
- **dogdoing.ai 的价格不是权威**：它只是聚合层，现价一律以交易所 API 为准；脚本会自动算偏差并与交易所价对照。
- **BTC 自身不做相对 BTC 强弱**（脚本已特判标注）。
- **免费接口拿不到的**：清算明细、链上交易所余额/巨鲸转账、精确解锁日历、合规/审计现状 → 一律标注"未核验"，不得推测。
- **数据源就两类**：交易所 API + dogdoing.ai。**不要为了补齐字段去接 CoinGecko / alternative.me 或其他源**（用户明令）；缺就标注"未取到/未核验"。因此**没有任何全网口径数据**（全网市值、BTC 占比、稳定币净流入）→ 报告里不得出现这些结论。
- **链上代币口径 ≠ 全网口径**（最容易误导的一条）：dogdoing 的 `token-info` 给的是**该链上代币**（如 BSC 上的 BTCB）的市值/持有人/流动性。**BTC 在 BSC 上的市值仅 $5.6B，而全网是万亿级**——把它当标的总市值写进结论就是重大错误。脚本已输出 `scope_warning`，主流币必定触发。
- **预测市场分布可能不完整**：dogdoing 返回的 outcome 报价合计常远低于 100%（实测 1.5%–5.0%）→ 此时**只能看相对排序，禁止当绝对概率**；脚本用 `distribution_valid` 自动标注。
- **dogdoing 是内部 JSON 接口**（非官方 API、无文档）：某天改结构/404 就 `--no-dogdoing` 降级并在报告标注"聚合层未取到"，**绝不编造热度/情绪数据**。
- **新闻类数字会随取数时间轮换**：报告引用的新闻（如"某次爆仓 7.5 亿美元"）在下一次取数时可能已滚出新闻列表，导致审计报"待确认"。正确处理 = 把该数字加入 `--extra` 并注明出处（媒体 + 日期），**不是删掉**；同时报告里引用新闻必须带时间与来源。
- **两处杠杆别混**：`隐含杠杆 = 名义头寸 ÷ 权益`（仓位大小的结果，高波动币常 <1x）与 `交易所杠杆`（平台设定值，决定保证金与强平距离）不是一回事；报告必须分别写明，并把算术校验收到的**最大安全杠杆**当作硬上限。

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

**加密交易报告额外自查**：
- [ ] 未出现任何股票口径概念（PE/PB/ROE/分析师目标价/护城河/管理层评分）
- [ ] 每档杠杆都做了爆仓距离校验（强平距离 ≥ 1.5×止损距离）
- [ ] 风险回报比实算并给出计算式；<2:1 有说明或直接驳回
- [ ] 资金费率正负方向解释正确，且计入持有成本
- [ ] 滑点与深度约束写成"可执行名义上限"
- [ ] 有 2–3 条可观测的失效条件（价格/资金费率/OI 阈值）
- [ ] 场所与稳定币风险有明确处置动作（降低交易所停留资金/分散托管）
- [ ] 链上/清算等未核验项已显式标注，无编造
- [ ] 数字审计已跑，待确认数字全部有出处
