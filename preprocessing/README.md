# 文档解析输入预处理

将 V1.0 邮件请求或 EML 转为材料清单、按内容保存的文件和可追溯的解析任务计划，并提供本地材料读取、有限字段提取和版本化结果交接入口。基础预处理不调用模型；本项目不提供异步业务服务或正式业务接口。

## 运行

需要 Python 3.11+ 和 `uv`。在本目录执行；锁文件记录本次验证使用的依赖版本。

```powershell
uv sync --locked --cache-dir .uv-cache
uv run --no-sync doccase-preprocess examples/inline-request.json --output output/demo
```

EML 输入显式提供上游标识：

```powershell
uv run --no-sync doccase-preprocess message.eml --task-id local-001 --email-ref-id mail-001 --output output/mail
```

命令输出 `state`、清单路径和材料数量。退出码 `0` 表示本次预处理可交接，`1` 表示有材料受阻，`2` 表示输入结构不合法。文件系统故障和程序异常直接报错。示例是合成邮件，使用保留域名，不代表真实业务验收。

输出目录包含：

- `objects/<sha256>`：取得的字节，文件名不来自邮件附件标签。
- `manifests/<run-id>.json`：本次材料出现记录、邮件角色、缺口、可得范围、解析任务和配置。重复运行保留各次记录，不覆盖历史。

内容文件在同目录临时文件中完整写入并关闭后，以硬链接原子发布到哈希路径；输出文件系统须支持硬链接。写入失败继续报错，不留下可复用的半成品；已存在但哈希不符的文件仍报错，不自动覆盖。

清单中的 `content` 指实际取得的表示；只有确实取得文件字节时才有 `original`。上游 `body_text`／`content_text` 标记 `provided_text`，不会假装是原始 PDF 或工作簿。EML 原文单独保存；转成 UTF-8 的文本标记 `derived_text`，保留父材料关联。JSON `body_html` 也作为 `derived_text` 交接，`original` 为空；JSON 字符串本身不提供原始 EML 字节。

两种入口生成的 UTF-8 HTML 均带一个 BOM，防止下游按 HTML 内遗留的其他编码声明读成乱码；正文、标签和引用保持不变。BOM 不参与正文判空，但计入实际字节数。下游文本读取可使用 `utf-8-sig`；表示层字符数不是原件偏移映射。

`ready` 仅表示可交给下一阶段；`parsing_completed` 和 `business_fields_verified` 始终为 `false`。任务里的 `execution_state=not_started` 不得映射成对外解析成功。

## 输入与接入

`adapt_request` 接收 V1.0 的 `task_id`、`email`；不要求上游补齐评审 Schema 的所有扩展字段。附件支持内联文本、严格 base64，或受控 `oss_url` 引用。缺少内容、格式损坏和读取失败留在清单中，其余材料继续准备。

替代正文只从可用表示中选择，损坏项仍保留异常。每封邮件独立核对 HTML 资源位置中的 CID，并解码字符实体与 URL 转义；正文文字及注释中的 `CID:` 不作为资源引用。没有对应材料的引用登记为缺件，正常正文仍可交接，不自动下载补齐。

上游已确认的上下文可以放在内部扩展 `context` 中：`ancestors` 为已排序的祖先邮件，`supplementary` 为补充邮件，`issues` 为上游缺链等异常。材料保留 `current/ancestor/supplementary` 角色；本包不根据主题或 References 重建线程，也不判断当前业务动作。

```python
from doccase_preprocessing.inputs import adapt_request
from doccase_preprocessing.models import ParserConfig
from doccase_preprocessing.pipeline import prepare

bundle = adapt_request(request)
manifest = prepare(
    bundle,
    "output/run",
    parser_config=ParserConfig(version="your-verified-docling-version", options={"do_ocr": True}),
    fetch_attachment=authorized_resolver,
)
```

`authorized_resolver(locator, byte_limit) -> bytes` 由部署接入层提供，负责身份授权、允许的下载目标、超时和流式字节限制。传入上限取单件上限与剩余累计材料额度的较小值，额度用完不再调用获取器。获取器应返回完整内容或报超限，不能截断后返回成功。到期、无权限、超时等已知获取失败应抛出 `MaterialError(code, message, **details)`，消息不含凭据或签名参数。未知异常原样传播。CLI 不抓取网络资源；未配置获取器的授权附件记录 `acquisition_not_configured`，普通正文链接不会下载。

每个解析任务列出全部 `material_ids`，下游据此回查每次出现的邮件角色和原件。任务标识包含内容 SHA-256、格式、路由和解析器配置；同内容可共用任务，同名不同内容保持独立。提供原件时会核对声明的大小及可选 SHA-256。这里没有解析结果缓存，也不保证业务 `task_id` 幂等：生产受理、重试、回调和持久化复用 AaaS 的既有 Task 服务。

## P01 本地交接核验

`doccase_preprocessing.local_handoff.run_local_handoff` 是内部本地接线入口：接收原始 JSON 或 EML 字节和调用方提供的受控请求引用，在适配前按原始字节计算请求指纹并暂存原件引用和上游问题详情，再生成现有清单并从磁盘交给 `receive_handoff`。受理上下文包含本次清单的 SHA-256，防止相同任务号和材料位置的不同受理记录串接；不改变清单结构。接收端显式接收 `intake_context`、预期任务和请求指纹，以及可信内容根目录；校验材料和任务引用、内容路径与 SHA-256，不执行解析任务。受理上下文仅存在调用进程内，不写进清单或模型提示；调用方不得将其中的受控引用和问题详情写入日志。

`content_text` 与 `oss_url` 对应同一材料时，`content` 是可用文本，`original` 仍为空。引用缺失、请求或来源位置不匹配时不使用原件引用，但不丢弃可读文本。已取得的 HTTP(S) 受控引用如在清单元数据或问题消息中重复出现，会以明确标记隐藏，原始问题详情仍保留在受理上下文；材料内容字节不改写。受控引用不能用作任务、材料标识或材料标签。已验证内容文件的哈希不代表已取得或核验原件，更不代表业务字段准确。真实引用的授权获取、跨进程传递和持久化由生产接入方实现。

在本目录可运行三个合成样例；缺件样例直接使用仓库中的 `architecture-integration/fixtures/partial-request.json`：

```powershell
uv run --no-sync python examples/p01_local.py --output output/p01-acceptance
```

每次运行新建独立目录；其中 `tampered-example` 是用于核验哈希错误的故意篡改样例。测试使用该集成样例的本地副本，以便预处理包独立运行。

## 本地工作台

可使用 [本地解析工作台](local_web/README.md) 选择邮件和文件、查看解析内容并回查字段来源。 当前功能和未覆盖范围见 [本地解析能力与限制](docs/local-parse-capabilities.md)。

## 模块

材料结果的校验、快照、版本修正及材料变更重跑入口，见[本地材料结果与交接](docs/result-handoff.md)。`complete` 仅表示调用方声明的范围已完成，不代表原件、业务正确性或正式接口验收；业务草案投影仅使用合成示例验证。

本地 `POST /local/parse` 的合成样例、索引选件和字段范围见[本地索引接口说明](docs/local-parse-index.md)。PDF 与附件货行的当前能力和限制见[本地解析能力与限制](docs/local-parse-capabilities.md)。

| 文件 | 职责 |
| --- | --- |
| `src/doccase_preprocessing/inputs.py` | V1.0／EML 适配，表示、来源角色、正文替代关系和 CID 登记 |
| `src/doccase_preprocessing/inspection.py` | 格式、可打开性、文件范围与资源检查 |
| `src/doccase_preprocessing/pipeline.py` | 字节存储、逐件问题、共用任务及清单交接 |
| `src/doccase_preprocessing/local_handoff.py` | 本地受理上下文绑定与清单接收核验，不运行解析 |
| `src/doccase_preprocessing/main_channel.py`、`rules.py` | 本地主解析、有限字段提取及共享的等重派生规则 |
| `src/doccase_preprocessing/weight_table.py` | 校验选定的表格、页或工作表、货行、列头和重量单位，区分整票、货行与单件口径 |
| `src/doccase_preprocessing/cargo_table.py`、`inquiry_details.py` | 货行、复杂表头与当前正文的字段候选和来源范围 |
| `src/doccase_preprocessing/semantic_context.py`、`agent_gap_evidence.py` | 待核字段的相邻 READ 片段和范围受限的候选证据 |
| `src/doccase_preprocessing/pdf_profile.py`、`examples/p02_local_read.py` | PDF 首读分流、受控读取与未读范围记录 |
| `examples/p02_markdown_export.py`、`src/doccase_preprocessing/markdown_body.py` | 从已读取结果导出 Markdown 和来源映射，处理正文历史引用边界 |
| `examples/p02_markdown_pool.py` | 受限 Markdown 工作进程复用与失败隔离 |
| `src/doccase_preprocessing/extraction_agent.py`、`material_agent.py` | 可选 Agent 候选、材料和字段范围及证据校验；失败或证据不足时保留原状态 |
| `src/doccase_preprocessing/agent_selection.py`、`group_pipeline.py` | 可选业务组选证和本地有界调度组件 |
| `src/doccase_preprocessing/group_context_agent.py`、`group_context_proofs.py` | 缺项上下文构造、证据绑定与结果合并；保护既有结果和首询归属 |
| `examples/local_web.py`、`examples/local_web_agent_context.py`、`examples/local_web_group_context.py`、`local_web/` | 本机工作台、单材料与业务组受限选证、单页 OCR 和来源展示 |
| `examples/deepseek_extraction.py`、`extraction_prompts.py` | DeepSeek 传输、选证输入和提取提示；默认不调用模型 |
| `src/doccase_preprocessing/output_consume.py`、`output_snapshot.py` | 独立输入校验、材料 JSON 原子保存与按版本回读 |
| `src/doccase_preprocessing/output_revision.py`、`output_reprocess.py` | 固定证据修正与变更材料重新准备、读取、提取 |
| `src/doccase_preprocessing/business_result.py` | 固定调用方提供的业务决策与材料版本，记录映射缺失的受阻状态 |
| `src/doccase_preprocessing/output_project.py` | 使用合成接口示例保存受限草案预览，不能表达的状态明确阻断 |
| `src/doccase_preprocessing/emailbridge_local.py` | 将已验证的完整文本范围接到 EmailBridge PoC，核对确认结果及明确的字段映射 |
| `src/doccase_preprocessing/models.py` | 最小内部类型、限制和异常契约 |
| `src/doccase_preprocessing/cli.py` | 本地运行入口和退出状态 |

基础包依赖 `pypdf` 和 `Pillow`，用于 PDF 与图片可读性检查；OOXML 使用标准库读取包结构。基础预处理不安装或运行 Docling，本地 P02/P03 读取示例可调用外部配置的 Docling 环境。未引入数据库、队列、重试框架、备用模型或公共 API 兼容层。

Excel 补读回归测试使用开发依赖 `openpyxl`。P02 示例的本地路径可通过 `DOCCASE_P01_REPORT`、`DOCCASE_INVENTORY`、`DOCCASE_DOCLING_PYTHON`、`DOCCASE_MODEL_CACHE` 和 `DOCCASE_NATIVE_PDF_ID` 配置；实际样本与这些配置值应保留在本机。

## 限制与验证

默认本地配置：入口及累计材料 20 MiB、单件 20 MiB、200 项材料、OOXML 2000 个成员／64 MiB 展开字节、1000 页 PDF、4000 万图片像素。通过 `--limits limits.json` 覆盖 `Limits` 字段。它们是有界读取的本地默认值，未经过生产容量测试；20 MiB 的报文不能据此直接送入当前 AaaS 1 MiB 路由。

PDF、XLSX、DOCX、PNG/JPEG、UTF-8 文本／HTML完成输入检查并准备任务。旧 XLS 只登记转换需求；普通压缩包、嵌套 EML 等不会自动展开。格式检查不运行公式、宏、HTML 脚本或外部关系。隐藏工作表等范围信息仅登记，不声称已完成内容解析。签名、未知图片和业务图片均保留；纯装饰图分类及跳过字段抽取属于后续规则。

```powershell
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv build --offline --cache-dir .uv-cache
```

Docling 序列化集成测试需要另行设置 `DOCCASE_DOCLING_PYTHON` 指向已安装 Docling 的 Python；未设置时这些测试会跳过。涉及模型的本地读取还需要 `DOCCASE_MODEL_CACHE` 指向已准备的缓存目录。

实现边界和接入工作见[开发交接说明](docs/engineering-handoff.md)。真实 OSS 授权、Docling 全格式内容读取、长上下文与字段准确率、生产部署和 SLA 尚未验证。
