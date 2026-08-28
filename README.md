# Investment Analysis 技能

用一套对冲基金投研级别的**多角色分析框架**，对某只股票产出**机构级投资备忘录 (Investment Memo)**。跨 Hermes / Claude / GPT 三端通用。

## 模块

- **基本面全流程**：分析师团队情报 4 份报告 → 投研团队多空辩论 → 交易主管执行计划 → 首席投资官审批。
- **技术面深度分析**：风险前置免责声明 → 量化数据仪表盘 → 多维时间框架共振 → 相对强度与波动率 → 交易剧本与风险矩阵 → 最终建议。

## 目录结构

```
investment-analysis/
├── SKILL.md                     # Hermes 技能说明(触发条件/流程/注意事项)
├── README.md                    # 本说明
├── references/
│   ├── fundamentals.md          # 基本面模板(带解析说明的版)
│   └── technical.md             # 技术面模板(带解析说明的版)
└── prompts/
    ├── fundamentals-prompt.md   # 纯 Prompt 正文(首行=总体指令)
    └── technical-prompt.md      # 纯 Prompt 正文(首行=角色)
```

## 用法(三端通用)

### Claude / GPT(或任何大模型)
直接把 `prompts/fundamentals-prompt.md` 或 `prompts/technical-prompt.md` 的**全文**粘贴进对话，然后把其中的 `[股票代码]` / `【市场/交易所】` 替换为目标标的即可。两个文件的首行就是 Prompt 正文，无任何 Markdown 包装，复制即用。

### Hermes Agent
本仓库即标准 skill 目录（`SKILL.md` + `references/`）。加载后，对 Hermes 说“分析/调研/研究某只股票”即自动套用；Hermes 会用真实工具抓行情、财报、分析师评级、13F 持仓等数据落地，而非空泛套话。

### 落地要求
- 数据一律来自真实抓取来源（`web_search` / `web_extract` / `browser_exec`），**禁止编造数字**。
- 报告开头必须保留免责声明，明确“不构成投资建议”。
- 技术指标结论仅作概率性推演，需声明局限性。

## 源码托管

模板原文整理自 **Lucas (htxlucas)**。本仓库为公开托管；Hermes 实际在 `~/.hermes/skills/finance/investment-analysis/` 加载。
