# DocCaseShow

用于展示和本地验证邮件正文及附件的文档解析效果。工程保存材料和读取结果，提取带来源的字段候选，供开发联调和人工核对；正式业务接口与生产部署尚未验收。

## 快速开始

需要 Python 3.11+ 和 `uv`。在 Windows PowerShell 中，从仓库根目录运行合成输入：

```powershell
Set-Location .\preprocessing
uv sync --locked --cache-dir .uv-cache
uv run --no-sync doccase-preprocess examples/inline-request.json --output output/demo
```

命令输出预处理状态、材料清单路径和数量；`parsing_completed: false` 表示尚未执行文档读取或字段验收。`output/` 保存本地运行数据，不提交到 Git。

## 本地联调入口

| 入口 | 用途 | 准备与操作 |
| --- | --- | --- |
| 本地工作台 | 导入邮件或文件，按材料和业务组核对原文、字段及来源 | [工作台启动与使用](preprocessing/local_web/README.md) |
| 本地 HTTP 接口 | 使用合成样例，或按本地索引选择已授权的邮件与 PDF | [索引接口与请求示例](preprocessing/docs/local-parse-index.md) |
| Python 预处理入口 | 将 JSON／EML 转成材料清单和读取任务 | [预处理说明](preprocessing/README.md) |

PDF、图片 OCR 和 XLSX 读取需要另行准备安装 Docling 的 Python 环境和本地模型缓存。服务只监听 `127.0.0.1`；真实邮件、附件、索引、样本标识、密钥和运行结果留在受控本地目录。

## 解析链路

`JSON / EML / 文件` → 材料预处理 → Docling READ → Markdown 与来源映射 → 字段提取及校验 → 结果保存与回读

1. **预处理**：登记正文、附件及线程角色，保存内容与清单，记录缺件和受阻状态。
2. **读取**：按材料格式选择读取方式；本地索引接口对明确的纯扫描 PDF 可在首读使用 OCR，对装箱单可优先读取表格。失败和未读范围保留在结果中。
3. **提取**：从 READ 或可回指的 Markdown 生成规则候选，按材料、页面、表格、多层或重复表头、货行、单位和数量口径校验。来源不足或冲突时保留待核状态。
4. **可选 Agent**：默认关闭；显式启用 DeepSeek 后按缺项选择材料、货票或货行证据，再由本地核验候选。业务组可进一步补充上下文，保留证据及货票范围；历史引用不自动升为首询。
5. **交接**：保存材料级结果、来源和版本；业务确认、正式接口映射和跨材料决策由后续接入负责。

各模块职责、配置入口、验证命令和已知限制见 [开发交接说明](preprocessing/docs/engineering-handoff.md)；当前字段与格式范围见 [本地解析能力与限制](preprocessing/docs/local-parse-capabilities.md)。

工作台可按正文与表格来源组装独立货票、货行和询盘结果；首询需人工确认，业务系统字段未接入时保持未连接。数据结构和边界见[询盘本地结构化结果](preprocessing/docs/inquiry-runtime-local.md)。

## 开发与验证

在 `preprocessing` 目录运行：

```powershell
uv run --no-sync pytest -q
uv run --no-sync ruff check src examples tests
uv run --no-sync ruff format --check src examples tests
```

需要运行 Docling 序列化集成测试时，设置 `DOCCASE_DOCLING_PYTHON` 指向已安装 Docling 的 Python；涉及模型读取时还需配置 `DOCCASE_MODEL_CACHE`。未配置时，依赖该环境的测试会跳过。

`ready`、READ 成功和字段 `known` 分别表示不同阶段的状态，均不等于业务验收。混合 PDF 页、复杂货行、独立精标覆盖、整组耗时及正式接口仍需继续验证；固定 READ 的定向联调不等于全格式业务验收。

## 仓库导航

- `preprocessing/examples/local_web.py`：本地工作台入口；`local_web/` 为页面。
- `preprocessing/src/doccase_preprocessing/`：材料预处理、字段提取、证据校验和本地结果交接。
- `preprocessing/tests/`：合成输入回归测试；真实材料和运行结果不在仓库中。
- `structured-output/integration-v03/snapshots/structured-output/evidence/`：本地接口和测试使用的输出 Schema。

当前实现和模块边界以[开发交接说明](preprocessing/docs/engineering-handoff.md)及代码为准；历史设计不能作为已完成或已验收的依据。
