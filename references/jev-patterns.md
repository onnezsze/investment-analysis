# 借鉴自 jev-trader 的 TypeSafe 决策范式

> 来源：**https://github.com/jarrodwatts/jev-trader**（jarrodwatts，TypeScript/Bun + Next.js）
> 定位：一个**实盘演示**——TypeSafe 的 Jev 模型在 Monad 链上交易所 Kuru 每 300ms（一个区块）回答一个问题："未来 N 个区块后 MON 会比现在高还是低？"拿到概率后由**代码**下单（post-only 限价单），并把每个区块的事件（决策概率、延迟、报价、成交、持仓、累计 PnL/成本）推给一个单页看板。
> 它自身的定位是"展示 AI 在做真实链上决策 + 推理成本低于 gas"，README 与 SPEC.md 都写明**这不是为了盈利的策略**。

## 一、它是什么（架构速览）

| 文件 | 职责 |
|---|---|
| `src/model.ts` | **Model 接口**：`decide(state) → {action, probabilities, upIn10, latencyMs}`。两个实现：`JevModel`（真实 TypeSafe，`experimental_evaluate`）与 `MockModel`（动量+失衡+均值回归的确定性替身，用于无 API 自测与压测） |
| `src/trader.ts` | 决策循环：**一区块一次、单请求在途、迟到即 hold**；`buildState()` 组装紧凑状态；`allowed()` 做仓位/保证金可行性钳制；事件落 `data/events.jsonl`；累计 `totals`（决策数、报价数、成交、回退、迟到、推理与 gas 成本、盈亏） |
| `src/book.ts` / `src/market.ts` | 读盘口（一次 `eth_call`）、批量撤单+挂单、保证金 |
| `src/server.ts` | SSE 直播：每个区块一条事件 |

**它的核心问题只有一个**：TypeSafe 的问题集里只有 `direction` 一个 Choice，`criteria` 明确写了**成本条件**（"mid 更高，且幅度要**超过点差**"）。所有风控、仓位、执行都在代码里。

## 二、可直接借鉴的 6 个模式（已落到我们的加密轨道）

| # | 借鉴点 | 它的做法 | 我们的落地 |
|---|---|---|---|
| 1 | **代码构建「相对化」状态，模型只做判断** | `buildState()` 只给 bps 收益（1/5/20/100 根）、盘口失衡 −1..1、按 5/10/25/50bps 的深度分档、顶部 5 档、成交摘要（CVD/VWAP/主动买卖）、近期成交字符串、`allowed` 标记 | `crypto_decide.py::build_state()` 同构：`trend_relative_bps`（4H/1H 的 bps 收益、RSI、ATR%、波动分位）、`market_structure`（价差/失衡/深度分档/基差）、`taker_flow`（CVD 与近期主动成交）、`positioning`（资金费率/OI/多空比/背离）、`structure_levels`、`cost`、`execution_constraints` |
| 2 | **结构化 instructions + criteria 写明成本阈值** | `instructions` 是对象 `{question, goal, timing, inputs}`，`inputs` 直接点名"taker flow 是最强信号"；`criteria` 写"要超过点差" | 同构：`direction` 的 `goal` 里嵌入**往返总成本 bps**，`inputs` 标注信号优先级；`criteria` 三个选项（long/short/no_trade）都带成本与空间条件 |
| 3 | **一次请求并行多个原子判断（composite scoring）** | 官方 `patterns/composite-scoring.md`：原子分 → 代码归一化 → 按画像加权 | 一次请求问 4 问：`direction`(Choice) + `crowding`(Score/5档) + `executability`(Score/5档) + `invalidation_clarity`(Noul)，代码侧归一化后按 `swing` / `scalp` 两套权重合成 conviction |
| 4 | **置信度门控（confidence-gated routing）** | 官方 `patterns/confidence-routing.md`：答案告诉你做什么，置信度告诉你是否该动；动作风险越高要求置信度越高，不足则交人 | `gate_and_size()`：`conf < 0.55`（swing）或 `0.65`（scalp）→ 观望；`0.55~0.75` → 半仓；`≥0.75` → 标准仓 |
| 5 | **代码拥有执行，模型只给意图** | 模型说 buy/sell，`allowed()` 才是能否下单的判据（仓位上限、保证金余额） | 代码侧硬约束：成本门槛（成本 > 典型波幅的 1/3 → 降级观望）、盘口深度是否支撑计划名义、**风险回报比 ≥2 硬门控**、波动率目标仓位、**爆仓距离 ≥1.5×止损距离** |
| 6 | **决策账本 + 结果回填** | 每区块事件落 `data/events.jsonl`，累计 `totals`（决策/成交/成本/PnL） | `decisions.jsonl`：每次决策记录 state_hash、概率、置信度、门控结论、`executed`、成本、计划（入场/止损/TP1/RR）；`--resolve` 回填 forward outcome（先触 TP 还是 SL、最大有利/不利偏移），并按「动作 × 置信度桶」统计命中率 |

## 三、刻意**没有**借鉴的部分（以及原因）

| 没抄 | 原因 |
|---|---|
| 300ms 级做市/挂单逻辑、post-only 报价、撤单替换 | 那是链上做市场景（赚点差、gas 成本可忽略、单区块结算）。我们要的是**方向性持仓**（数小时到数天），执行层应交给交易所 API 的限价单，不自己做市 |
| 单区块结算的延迟预算（硬编码 gas、fire-and-forget） | 与我们的决策周期无关；但**"延迟/迟到就不动手"**的精神被保留：数据陈旧或盘口深度不足即降级观望 |
| 看板/SSE 可视化、SPEC 里的展示型设计原则 | 那是"展示产品"，我们是"决策工具"。但"每笔都可核验、盈亏如实展示"的原则被保留在账本里 |
| `MockModel` 的动量启发式**当作信号** | 只当**管线替身**用于自测（`--model mock`），绝不作为交易依据 —— 实测中它与真实 Jev 的方向判断完全相反（见下） |

## 四、实测（2026-09-22，BTCUSDT，状态相近的两次调用）

| 运行 | 方向答案 | 概率分布 | 置信度 | 代码侧门控结论 |
|---|---|---|---|---|
| mock 替身 | long | long 0.73 / short 0.15 / no_trade 0.12 | 0.73 | 按半仓执行，RR 3.0 |
| **真实 Jev** | short | short 0.63 / no_trade 0.25 / long 0.12 | **0.45** | **观望**（置信度 < 0.55） |
| **真实 Jev**（同状态复跑） | long | — | **0.10** | **观望**（分布近乎平坦 = 无信号） |

**结论**：
1. **同一个状态，Jev 的动作会在 long/short 间翻转且置信度很低** → 说明在我们给的这个 4H 波段状态下，**模型自身认为没有可靠方向**。这正是"置信度门控"存在的意义：**如果按动作硬执行，这两次会开出方向相反的两笔仓**。
2. **启发式替身与真实模型的判断可以完全相反** → mock 只能验证管线，不能替代 TypeSafe。
3. **真正能收敛这个问题的只有账本**：把每次决策（含执行/否决）与后续 TP/SL 结果对齐，按置信度桶统计，才能判断"高置信度是否真的更准"。这是下一步要做的事，也是 jev-trader 里 `totals` 给我们的最大启发。

## 五、下一步（可选，按需再做）

- **累计样本**：定时跑 `crypto_decide.py`（cron），让账本自然积累；`--resolve` 到期回填，得到置信度分桶胜率。
- **autoresearch feature discovery**（官方 cookbook）：把状态里的字段转成数值特征，用回填结果做监督学习，找出**哪些指标真的预测了 TP/SL** —— 这比继续堆指标更有价值。
- **多画像并行**：官方 `patterns/fan-out.md` 的 speculative fan-out，可一次请求同时问 swing 与 scalp 两套问题，代码只消费适用的一侧。
