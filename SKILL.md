---
name: investment-analysis
description: "Institutional stock & crypto analysis: fundamentals, technicals & tokenomics."
version: 1.2.0
author: Lucas (htxlucas), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, investment, stock, analysis, template]
---

# 投资分析 (Institutional Investment Analysis) 技能

用一套对冲基金投研级别的多角色框架，对某只股票产出**机构级投资备忘录 (Investment Memo)**。包含两种可复用模板：**基本面全流程分析** 和 **技术面深度分析**。模板原文保留在 `references/` 下的文件中。

本技能负责：按用户选择套用模板、**用真实工具抓数据落地**（而非空泛套话）、并按模板角色产出结构化工件。它**不**构成投资建议——所有输出都必须在开头带免责声明。

## When to Use（触发条件）

- 用户说 "分析 / 看一下 / 调研 / 研究 某只股票"，要一份机构级别报告。
- 用户点名要用 **基本面** 或 **技术面** 模板。
- 用户给了股票代码/名称（美股、港股、A股均可），并要求多角色深度分析。
- 用户说 分析某加密代币/币种（BTC / ETH / SOL / 主流币 / Alt），要机构级报告 → 用 `references/crypto.md`（含 Tokenomics / 链上数据 / 衍生品 / 加密技术面）。
- 用户说 "用投资分析模板 / 跑一遍投研流程"。

**Don't use for:** 简单的行情查询（只需要当前价格/涨跌幅）；DCF/估值单一指标速算；不需要结构化报告的一次性问答。

## Prerequisites（前置条件）

- 需要用户提供：**市场/交易所**（可选，可推断）与**股票代码或名称**（必须）。缺代码时先问，不要猜。
- 分析所需的实时数据（价格、财报、分析师评级、持仓、新闻、技术指标）都要通过 `web_search` / `web_extract` / `browser_exec` 抓取真实来源，禁止凭空编造数字。

## How to Run（如何使用）

1. 确认用户要哪套模板，以及股票代码/名称（缺则问）。
2. **基本面全流程** → 加载 `references/fundamentals.md`；**技术面** → 加载 `references/technical.md`。
3. 用工具抓真实数据，逐角色填充报告（见 Procedure）。
4. 在报告开头加免责声明（原文已写在模板内）。
5. 输出为结构化的 Markdown 备忘录；中文/英文跟随用户习惯。

## Quick Reference

- 基本面模板：`references/fundamentals.md` — 分析师情报、多空辩论、交易执行、CIO 审批。
- 技术面模板：`references/technical.md` — 风险前置、量化仪表盘、多周期共振、相对强度、交易剧本。
- 加密模板：`references/crypto.md` — Tokenomics、链上数据、衍生品/资金费率/OI、ETF 与稳定币流向、加密技术面（vs BTC 相对强弱、BTC.D）。
- 跨端通用 Prompt：`prompts/fundamentals-prompt.md`、`prompts/technical-prompt.md` — 纯正文、复制即用，可直接粘给 Claude / GPT / 任何大模型。
- 关键工具：`web_search`、`web_extract`、`browser_exec`（抓新闻/财报/行情/机构持仓）。

## Procedure（执行步骤）

### 加密货币分析（crypto.md）
1. 确认币种（BTC/ETH/SOL/Alt）与市场（现货/合约、交易所）。
2. 用 `web_search`/`web_extract`/`browser_exec` 抓加密特有数据：CoinGecko（价格/市值/FDV/流通量）、解锁日历、资金费率/OI、ETF 与稳定币流向、链上数据、恐惧贪婪指数、监管/宏观新闻。
3. 按 crypto.md 产出：①通证与协议基本面 ②链上/衍生品资金面 ③新闻/宏观/监管 ④情绪资金流 ⑤加密技术面（相对强弱 vs BTC）。含多空辩论、执行计划、CIO 审批。
4. 数据必须真实回溯，禁止编造数字；重点核查爆仓/解锁/资金费率等加密特有指标。

### 基本面全流程（fundamentals.md）
1. **确认真实数据来源**：用 `web_search`/`web_extract` 取公司财务数据、财报、分析师评级、13F 持仓、行业与监管新闻、宏观指标、社交/员工口碑数据。
2. **分析师情报 (Part 1)**：按模板产出 4 份分角色报告——基本面、新闻宏观监管、情绪资金流、技术面。
3. **多空辩论 (Part 2)**：分别写看涨与看跌论点，要求互相反驳，每点都给数据支撑。
4. **交易执行 (Part 3)**：给出明确动作 (LONG/SHORT/STAY ASIDE)、仓位、入场/止损/止盈区。
5. **CIO 审批 (Part 4)**：计算风险回报比、做压力测试、给出批准/驳回结论。
6. 核对：每个量化论断都有来源，风险回报比计算正确。

### 技术面深度分析（technical.md）
1. **风险前置**：报告开头粘贴免责声明原文，不可省略。
2. **量化仪表盘**：用 `browser_exec`/`web_extract` 抓最新价格、52周高低点、均线、ATR、相对强弱、成交量。
3. **多周期共振**：周线/日线/4小时三层分别分析结构、形态、量价、指标共振。
4. **相对强度与波动率**：与 SPY/QQQ 对比，分析布林带宽度与 ATR 止损区间。
5. **交易剧本**：给出 Base/Bearish 两情景、入场区、止损、目标位。
6. 核对：价格/指标数字来自真实行情，止损与目标位与 ATR 匹配。

## Pitfalls（注意事项）

- **数据必须真实**：价格、财报、评级等数字一律来自抓取的真实来源；抓不到就明确标注"数据缺失"，绝不能编。
- **免责声明不可省略**：模板含免责声明，必须保留，且明确说明"不构成投资建议"。
- **股票代码缺失**：先确定再分析，不猜。
- 技术指标结论仅作概率性推演，需在报告中声明局限性。

## Verification（验证）

- 输出符合模板结构，每部分标题齐全。
- 所有关键数字均能回溯到抓取的真实来源。
- 基本面模板包含风险回报比计算与明确批准/驳回结论。
- 技术面模板以免责声明开头，且给出清晰的入场/止损/目标位。
