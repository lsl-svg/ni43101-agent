# NI 43-101 资源量抽取 Agent

> 交付物：从 NI 43-101 矿权报告 PDF 中自动抽取 Indicated / Inferred 资源量的
> 矿石量 (Mt)、品位 (g/t 或 %)、金属量 (oz / t)，并通过 **Extractor → CriticMaster → Revise Loop →
> Evolution Log** 闭环，保证「**抽错时拒答（abstain），而不是硬给**」。

**状态**：全链路可运行（含无需 API Key 的 mock 演示）；真实模型仅需填入 Key 即可切换。

---

## 1. 5 分钟验证（不需要任何 API Key）

```bash
pip install -r requirements.txt && pip install -e .
python -m ni43101.cli mock        # 或：docker compose run --rm agent python -m ni43101.cli mock
python -m ni43101.cli rerun --mock   # 复跑对比：Evolution Log -> few-shot 是否带来提升
```

用夹具驱动完整链路（Extract → Critique → Revise → Abstain → Eval），输出示例：

```
[ABSTAIN] newmont_placeholder: rounds=2 score=8.0 rows=2
        -> R2: 关键字段缺失比例 50% > 阈值 25%
        -> R4: inferred/Au.grade_value 在提供的节选中被截断，无法溯源；已置 null，待人工审核
[PASS] pilbara_placeholder: rounds=2 score=10.0 rows=3
完成 2 篇：放行 1，待人工审核 1
待人工审核队列: out/review_queue.jsonl
------------------------------------------------------------
案例数: 2
字段级准确率: 100.0%
硬给率: 0.0%  拒答召回: 100.0%
报告已写入: out/report.md
```

它同时演示了两种结局：
- `pilbara`：round1 评分 0（埋了单位错位 / 小数点错位 / 幻觉引用）→ 修订后 round2 评分 10 → 放行；
- `newmont`：原文节选被截断、无法溯源 → 修订把该字段置 null 并登记 blocker → **触发 abstain，不硬给**，
  并写入 `out/review_queue.jsonl`（待人工审核队列）。

> 不想跑命令、想直接看输出样例：`docs/sample_eval_report.md`（评测报告）与
> `docs/sample_ablation.md`（复跑对比）。

---

## 2. 交付清单对照（设计要求 → 实现位置）

| 设计要求 | 实现 |
| --- | --- |
| Extractor Agent（强模型抽取） | `src/ni43101/agents/extractor.py` + `prompts/extractor.md` |
| CriticMaster Agent（换一个模型挑刺，评分 1–10） | `src/ni43101/agents/critic.py` + `prompts/critic.md`（**异构模型** + 确定性规则层） |
| Revise Loop ≤3 轮，评分 ≥8 才通过 | `src/ni43101/pipeline.py`（`max_rounds` / `pass_score` 来自配置，不写死） |
| 否则进入 abstain + 标注待人工审核 | `src/ni43101/abstain.py`（可枚举的 R1–R6 规则）+ `out/review_queue.jsonl` 待人工队列 |
| Evolution Log（每次失败/降级自动 append） | `src/ni43101/evolution.py` → `out/evolution.jsonl` |
| 用 Evolution Log 做 few-shot 改进，**最后复跑** | `python -m ni43101.cli rerun`：基线（不注入）与复跑（注入 mine 出的样例）跑同一批数据、同一套 GT，输出 `out_ablation/ablation.md` 对比 |
| 评分协议：字段级 accuracy（±5%） | `src/ni43101/evaluation/metrics.py`（数值相对容差 ±5%，类别字段精确匹配） |
| 最看重：明显错误时是否 abstain | `metrics.py` 的**硬给率 / 不可确定字段被硬给值的次数** 两个指标 |

---

## 3. 架构

```
                 data/pdfs/*.pdf
                        │  pdfio.load_pages() + select_relevant_pages()   ← 关键词+表格打分选页，控制上下文预算
                        ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  ExtractorAgent (强模型, 结构化 JSON)                               │
   │      └─ normalize.canonicalize_raw_row()  ← 单位归一 + 补算金属量    │
   └────────────────────────────────────────────────────────────────────┘
                        ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  CriticMasterAgent = RuleCritic (确定性) ⊕ LLM Critic (异构模型)     │
   │      取严：规则层给评分上限，阻断项取并集                            │
   └────────────────────────────────────────────────────────────────────┘
              score ≥ 8 ?  ──是──▶  放行
                        │
                        否（且轮数 < max_rounds）
                        ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  ReviserAgent  ← 注入 Evolution Log 挖掘出的历史失败 few-shot        │
   │      只改被指出的字段；无原文依据则置 null + unresolved_blockers      │
   └────────────────────────────────────────────────────────────────────┘
                        ▼  （循环，最多 3 轮）
   ┌────────────────────────────────────────────────────────────────────┐
   │  abstain.evaluate()  R1–R6                                          │
   │      → out/<report>/final.json, round*.json, out/evolution.jsonl     │
   └────────────────────────────────────────────────────────────────────┘
                        ▼
              evaluation/  →  out/report.md（字段级 accuracy、硬给率、逐轮提升）
```

---

## 4. 四个核心设计决策

### 4.1 单位与量纲独立成层：这是本系统第一大错误来源
同一份 NI 43-101 里会同时出现 `Mt / kt / t`、`g/t / ppm / %`、`oz / Moz / koz / t`，换算差 10⁴~10⁶ 倍。
`normalize.py` 把两件事固定下来：

```
Au/Ag:  contained(t) = tonnage(Mt) × grade(g/t)        # 百万吨 × 克/吨 = 吨
Cu 等:  contained(t) = tonnage(Mt) × grade(%) × 1e4
ppm ≡ g/t
4.45 Moz = 4.45e6 oz = 138.41 t Au
```

同时**归一化必须在评测侧复用**：ground truth 也走同一个 `canonicalize_raw_row`，否则「GT 用 %、
预测用 g/t」会变成假错误（见 `evaluation/gt_adapter.py`）。另外，`Total/合计` 这类汇总行会被
`normalize_category()` 直接拒绝——把合计行当成一个类别抽出来是很常见的"看起来对、实际重复计数"错误。

> 开发过程中真实踩到的坑已固化为回归测试：金属量"吨"一度误用了吨位换算（往 Mt 收敛），
> 导致 `560000 t` 被当成 `0.56 t`、偏差 10⁶ 倍。见 `tests/test_normalize.py::test_tonnes_identity`。

### 4.2 双层评审：规则层管算术，LLM 层管语义
- 纯 LLM 评审**会漏机械性错误**：单位错位、`contained ≠ tonnage × grade` 这类问题它常判 ok；
- 纯规则评审**会漏语义错误**：编造的引用、跨类别串行、表格串列。
所以 `RuleCritic` 负责硬约束（幻觉溯源、单位可解析、量级合理区间、内部一致性）并给**评分上限**，
LLM 负责细致审查，最终**取严**。规则层不花 token、可单元测试（`tests/test_abstain.py`）。

### 4.3 拒答是可枚举的规则，不是模型的自由裁量
| 规则 | 触发条件 |
| --- | --- |
| R1 | 用尽 max_rounds 后评分仍 < pass_score |
| R2 | 关键字段（tonnage / grade）缺失比例 > 25% |
| R3 | `contained` 与 `tonnage × grade` 偏差 > 5% |
| R4 | 仍有 blocking_issues，或 Reviser 返回 unresolved_blockers |
| R5 | 一行都没抽出来 |
| R6 | 量级或"矿种-单位"匹配性可疑 |

命中任一 → `abstain=True` + `needs_human_review=True` + 给出**可读的拒答理由**。
阈值全部在 `config/settings.yaml`，代码里没有硬编码。

### 4.4 Evolution Log 不只是日志，是**复跑**的依据
`evolution.jsonl` 记录 extract / critique / revise / final 四个阶段，其中 revise 记录保存
`before_rows` 与 `after_rows`；`mine_few_shot()` 从中挑出失败样例注入下一轮的 Reviser prompt ——
这就是设计要求的"用这个 log 做 few-shot 改进，最后复跑"。

`python -m ni43101.cli rerun --mock` 做的是严格对照实验：基线跑（`few_shot_k=0`）与复跑（`few_shot_k=2`）
**共用同一份 evolution.jsonl、同一套 GT**，结果（`out_ablation/ablation.md`）：

| 报告 | 基线准确率 | 复跑准确率 | 基线评分 | 复跑评分 | 基线决策 | 复跑决策 |
| --- | --- | --- | --- | --- | --- | --- |
| newmont_placeholder | 100.0% | 100.0% | 6.0 | 8.0 | 拒答 | **拒答（保持不变）** |
| pilbara_placeholder | 88.9% | **100.0%** | 0.0 | 10.0 | 拒答 | 放行 |

总体：字段级准确率 92.6% → **100.0%**；不可确定字段被硬给次数 2 → **0**。
值得强调的是 `newmont`（GT 标注 `expect_abstain`）在复跑后**仍然拒答** ——
如果 few-shot 让该拒答的案例也"放行"了，那是退化而不是提升。这条负向对照也写在了报告里。

---

## 5. 评测口径（`out/report.md`）

- **数值字段**（tonnage_mt / grade_value / contained_t）：相对容差 ±5%；
- **类别字段**（category / commodity / grade_unit）：归一化后精确匹配；
- **漏抽**（GT 有、预测 null）与**多抽**（预测有、GT 无对应行）都计为错误——多抽的行会被
  记为 `spurious`，直接体现"编造数据"的代价；
- **GT 中为 null 的字段不进准确率分母**（属于"不可确定"），而是进入拒答指标；
- **硬给率**：GT 标注 `expect_abstain` 却未 abstain 的比例；
- **不可确定字段被硬给值的次数**：GT 标了 `unresolvable_fields`（如原文截断）而系统仍给出具体值
  的次数 —— 这是"是否硬给"最直接的证据，也是评测的重点。

> mock 演示：字段级准确率 100%、硬给率 0%、拒答召回 100%、不可确定字段被硬给 0 次。
> **注意：mock 夹具中的数字是为演示而造的占位值，不是真实披露数据。**
>
> **数据说明**：当前仓库内跑的是**自造夹具**（两个 placeholder 案例，故意埋了单位错位、品位小数点
> 错位、幻觉引用与"原文被截断"四类问题），用于在**不依赖任何 API Key** 的前提下验证全链路。
> 客户提供的 3 份真实 NI 43-101 与 ground truth JSON 尚未下发；接入路径已就绪——把文件放进
> `data/pdfs/` 与 `data/ground_truth/` 后执行 `run --all` + `eval` 即产出真实指标，**无需改代码**
> （GT 字段名不同时按第 6 节加一行别名）。

---

## 6. 换成真实数据

1. 把需求方给的 3 份 PDF 放进 `data/pdfs/`，GT JSON 放进 `data/ground_truth/`（同名即自动配对）；
2. 复制 `.env.example` 为 `.env`，填入三个模型 Key（Extractor 用强模型、Critic 换一家厂商）；
3. 跑：

```bash
python -m ni43101.cli run --all
python -m ni43101.cli eval          # 生成 out/report.md
python -m ni43101.cli stats         # 打印 Evolution Log 统计
```

GT 字段名与本项目不一致时，只需在 `evaluation/gt_adapter.py::FIELD_ALIASES` 加一行别名，
评测逻辑无需改动。没有 pdfplumber 的环境可把 PDF 转成同名 `.txt` 放在 `data/txt/`。

---

## 7. 已知局限（故意写清楚，避免"演示级"被误读为"生产级"）

1. **选页启发式**：`select_relevant_pages` 用关键词+表格密度打分，跨页续表可能被截断；
   正确解法是按表格 caption 做版面检索（或接入版面模型），当前未做。
2. **超长报告**：单页超长时仍可能超上下文，未做分块+聚合抽取。
3. **多品位版本**：同一类别存在多套 cut-off 时，依赖模型把每套都输出并在 notes 标注；
   规则层目前只检查一致性，不校验 cut-off 完整性。
4. **规则区间是经验值**：`PLAUSIBLE_GRADES` 覆盖常见矿种，稀有矿种需补表。
5. **未做**：人工复核 UI、增量处理、并发与限流、缓存（本次未要求）。

---

## 8. 目录结构

```
config/settings.yaml      全部阈值与模型配置（无硬编码）
prompts/                  extractor / critic / reviser 三份系统提示词
src/ni43101/
  schemas.py              领域 schema（规范单位 + 可审计字段）
  normalize.py            单位/量纲归一化 + 合理区间校验 + 汇总行拒绝
  abstain.py              拒答规则 R1–R6
  evolution.py            Evolution Log（写 + few-shot 挖掘 + 统计）
  llm.py                  OpenAI 兼容多 provider 客户端（重试 / 兜底 JSON 解析）
  pdfio.py                PDF 读取 + 相关页筛选
  pipeline.py             主编排；单篇失败降级为拒答，不中断整批；拒答写入待人工队列
  runner.py               组装 Agent + 批量执行（CLI 与 ablation 复用）
  ablation.py             复跑对照实验（基线 vs 注入 few-shot）+ 对比报告
  cli.py                  run / eval / rerun / mock / stats
  mock.py                 夹具 Agent（无 Key 可跑）
  evaluation/             评测层：gt_adapter / metrics / report / run_eval
data/{pdfs,txt,mock,ground_truth}/   数据与夹具
tests/                    28 个单元测试（stdlib unittest，无额外依赖）
out/                      运行产物：final.json、round*.json、evolution.jsonl、review_queue.jsonl、report.md
out_ablation/             复跑对照：baseline/ 与 fewshot/ 两套产物 + ablation.md
```

## 9. 测试

```bash
python -m unittest discover -s tests     # 28 passed
```
覆盖：单位换算与 Moz 倍数、汇总行拒绝、一致性判定、拒答 R1–R6 正反用例、±5% 容差边界、
漏抽/多抽统计、"不可确定字段被硬给"的检测。
