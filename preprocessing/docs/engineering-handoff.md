# 文档解析开发交接

本文对应仓库中的本地实现，供开发者定位入口和继续联调；设计稿、真实样本记录和业务验收结论分别维护。

## 实现与职责

| 阶段 | 主要文件 | 职责 |
| --- | --- | --- |
| 输入与材料 | [`inputs.py`](../src/doccase_preprocessing/inputs.py)、[`inspection.py`](../src/doccase_preprocessing/inspection.py)、[`pipeline.py`](../src/doccase_preprocessing/pipeline.py) | 适配 JSON／EML、检查格式、保存内容，记录材料角色、缺口和读取计划。 |
| 本地交接 | [`local_handoff.py`](../src/doccase_preprocessing/local_handoff.py) | 绑定请求与清单，校验路径和内容摘要；`ready` 表示可继续交接。 |
| READ | [`p02_local_read.py`](../examples/p02_local_read.py)、[`p02_docling_worker.py`](../examples/p02_docling_worker.py)、[`pdf_profile.py`](../src/doccase_preprocessing/pdf_profile.py) | 在受控 Docling 进程中读取材料；PDF 按识别结果选择原生、表格或纯扫描 OCR 首读，保留超时和未读范围。 |
| Markdown | [`p02_markdown_export.py`](../examples/p02_markdown_export.py)、[`p02_markdown_pool.py`](../examples/p02_markdown_pool.py) | 从已保存 READ 导出 Markdown 与位置索引；受限工作进程失败时不发布不完整结果。 |
| 字段与证据 | [`main_channel.py`](../src/doccase_preprocessing/main_channel.py)、[`cargo_table.py`](../src/doccase_preprocessing/cargo_table.py)、[`weight_table.py`](../src/doccase_preprocessing/weight_table.py)、[`inquiry_details.py`](../src/doccase_preprocessing/inquiry_details.py) | 提取正文和附件候选，分别校验字段、货行、表头、数量范围、重量口径和来源。 |
| 正文货票与货行 | [`body_objects.py`](../src/doccase_preprocessing/body_objects.py) | 从已验证正文 READ 的段落和表格构建独立对象，保留位置、引用角色和同票关联状态。 |
| Agent | [`material_agent.py`](../src/doccase_preprocessing/material_agent.py)、[`extraction_agent.py`](../src/doccase_preprocessing/extraction_agent.py)、[`agent_selection.py`](../src/doccase_preprocessing/agent_selection.py) | 选择可回查证据、限制业务组请求范围，验证返回候选；默认不请求外部模型。 |
| 对象级补提 | [`object_agent.py`](../src/doccase_preprocessing/object_agent.py)、[`local_web_agent_context.py`](../examples/local_web_agent_context.py) | 按货票或货行的缺项选择受限证据，核对候选对象、字段、单位和证据绑定。 |
| 模型适配 | [`deepseek_extraction.py`](../examples/deepseek_extraction.py)、[`extraction_prompts.py`](../examples/extraction_prompts.py) | 显式启用时向 DeepSeek 发送选定证据；提示词和模型输入构造独立维护。 |
| 上下文选证 | [`semantic_context.py`](../src/doccase_preprocessing/semantic_context.py)、[`agent_gap_evidence.py`](../src/doccase_preprocessing/agent_gap_evidence.py)、[`local_web_agent_context.py`](../examples/local_web_agent_context.py) | 针对待核字段组合当前材料中相邻的读取片段；保留片段次序和来源，排除不适用内容。 |
| 组级补提 | [`group_context_agent.py`](../src/doccase_preprocessing/group_context_agent.py)、[`group_context_proofs.py`](../src/doccase_preprocessing/group_context_proofs.py)、[`local_web_group_context.py`](../examples/local_web_group_context.py) | 只为缺项收集受限证据，核对同票和来源后输出补充；失败或超时保留首轮结果。 |
| 本地询盘结果 | [`inquiry_runtime.py`](../src/doccase_preprocessing/inquiry_runtime.py)、[`derived_fields.py`](../src/doccase_preprocessing/derived_fields.py) | 依据人工确认的首询和来源绑定结果组装邮件头、货票和货行；有同范围依据时计算比重等字段。 |
| 结果与业务组 | [`output_consume.py`](../src/doccase_preprocessing/output_consume.py)、[`output_snapshot.py`](../src/doccase_preprocessing/output_snapshot.py)、[`group_pipeline.py`](../src/doccase_preprocessing/group_pipeline.py) | 校验和回读材料结果；组级调度通过 Python 回调分别处理受理、读取、选证、模型和收尾。 |

本地 HTTP 入口为 [`local_parse_server.py`](../examples/local_parse_server.py)，工作台入口为 [`local_web.py`](../examples/local_web.py)。组级调度和缺项选证提供内部 Python 组件，不代表工作台或正式服务已自动接入批处理。

工作台的 [`local_web_pipeline.py`](../examples/local_web_pipeline.py) 负责读取结果和单元格证据关联；[`local_web_measurements.py`](../examples/local_web_measurements.py) 负责在明确的单件包装条件下展示尺寸估算，估算值不自动成为整票体积。

[询盘本地结构化结果](inquiry-runtime-local.md)说明首询确认、字段来源和业务系统未连接状态；该本地结果不替代正式业务接口或已批准映射。

工作台对当前正文中的内嵌签名图片进行发件地址与读取来源核验；Excel 显示精度和计算精度分别保留。PDF 货行净重的位置文本补充仍要求同页、唯一匹配。

独立接口可显式启用图片 OCR；[`inline_company.py`](../src/doccase_preprocessing/inline_company.py) 和 [`local_inline_company.py`](../examples/local_inline_company.py) 核验当前正文 CID、发件域名及图片来源后，才可将公司候选写入新的材料交接快照。默认不启用。

## 读取与字段边界

- 索引接口先检查 PDF 的扫描状态。明确的纯扫描件可走 OCR 首读；装箱单可走表格首读；安全数据表和无法确认的情况沿已有分流。Markdown 导出不完整本身不会触发 OCR。
- 混合文字与扫描图片的 PDF 仍需逐页核查；工作台提供手动选择页面补 OCR。读取失败、超时和未读页不能按空白字段当作成功。
- Markdown 片段须能对应同一份 READ；映射不足时，可按调用方配置使用同材料 READ 证据，仍不足时保留未知。历史转引、回复和附件不自动替代首询主值。
- 表格字段需验证页或工作表、表、行、多层或重复列头与单位；跨页重复表头按所在页检查。商品数量、包装件数和箱数，以及单件、货行、整票重量分别处理；不满足证据条件时留空。
- 待核字段的相邻片段选证受当前材料、正文范围和敏感内容过滤约束；工作台展示 PDF 货行与来源，以及未分类的重量、件数线索。启用组级补提后，包装类型、整票体积、业务备注和整票毛重可追加一次上下文请求；只发布为补充，不覆盖首轮，也不把历史引用升为首询。
- 组级调度可选 `read_group_sizes` 调整组尾读取优先级，调用方须提供完整材料数量并为模型请求设置期限。该策略尚未证明改善尾部耗时，不是默认性能承诺。

## 联调与继续验证

先按[首页](../../README.md)运行合成输入，再根据[本地工作台说明](../local_web/README.md)或[索引接口说明](local-parse-index.md)配置独立 Docling 环境、模型缓存和本地材料。启用 DeepSeek 必须显式配置；组级上下文补提还需设置 `--group-context-fallback`。不要将密钥写入命令历史或仓库。

字段变更先运行相关 `tests/`，再执行首页的完整 `pytest` 与 Ruff 检查。Docling 集成测试依赖单独配置的 Python 环境；自动化测试、真实材料回放和业务验收应分别记录。

下一阶段需补齐未参与规则调优的多行 PDF、扫描装箱单和 Excel 货行正例，逐项核对来源与单位；再用同一 READ 对比规则和 Agent 的新增命中、误填与耗时。正式评测需固定逐封关键字段，分别统计字段准确率、召回率、整封通过率及平均处理耗时；无输出和读取失败仍计入相应评测范围。
