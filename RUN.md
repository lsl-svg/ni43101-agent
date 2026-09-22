# RUN —— 5 分钟内跑起来

## 方式 A：Docker（推荐，一条命令）

```bash
cp .env.example .env      # 仅跑 mock 时可留空
docker compose run --rm agent python -m ni43101.cli mock
docker compose run --rm agent python -m ni43101.cli rerun --mock
```

`docker-compose.yml` 里 image、卷、env_file 都已配好；`out/report.md` 会直接出现在宿主机 `./out/`。

## 方式 B：本机 Python 3.10+

```bash
pip install -r requirements.txt
pip install -e .
python -m ni43101.cli mock          # 无需 Key，验证全链路
python -m ni43101.cli rerun --mock  # 无需 Key，复跑对照（few-shot 是否带来提升）
```

## 跑真实数据

```bash
# 1) 放数据
#    data/pdfs/*.pdf          需求方给的 3 份 NI 43-101
#    data/ground_truth/*.json 同名 GT（文件名 = 去掉扩展名的 PDF 名）
# 2) 配 Key（Extractor 强模型 / Critic 换一家厂商 / Reviser 任意）
cp .env.example .env
# 3) 跑
python -m ni43101.cli run --all
python -m ni43101.cli eval
```

## 我实际执行的命令与结果（可复现）

```bash
$ python -m unittest discover -s tests
Ran 28 tests in 0.002s
OK

$ python -m ni43101.cli mock
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

$ python -m ni43101.cli rerun --mock
... (基线：两篇均拒答，准确率 92.6%；复跑：pilbara 修复后放行，准确率 100.0%)
mine 到修复样例: 6
基线准确率: 92.6%
复跑准确率: 100.0%
基线拒答 2 -> 复跑拒答 1
对比报告: out_ablation/ablation.md
```

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `python -m ni43101.cli mock` | 夹具数据跑全链路 + 出评测报告（无需 Key） |
| `python -m ni43101.cli rerun --mock` | 复跑对照：Evolution Log → few-shot，看指标是否提升（无需 Key） |
| `python -m ni43101.cli run --all` | 真实模型 + `data/pdfs` 全量 |
| `python -m ni43101.cli run --report-id <id>` | 只跑单篇 |
| `python -m ni43101.cli eval` | 只重算评测（读 `out/` 与 GT） |
| `python -m ni43101.cli stats` | 打印 Evolution Log 统计 |

## 排障

| 现象 | 处理 |
| --- | --- |
| `缺少模型凭证` | 填 `.env`；或先用 `mock` 验证链路 |
| 未找到待处理报告 | 检查 `data/pdfs/` 是否有 PDF，或 `data/mock/*.source.txt` |
| 无 pdfplumber | 把 PDF 转成同名 `.txt` 放 `data/txt/`，程序会自动回退 |
| Windows 控制台中文乱码 | 已在 `cli.main()` 里把 stdout 切到 UTF-8 |
