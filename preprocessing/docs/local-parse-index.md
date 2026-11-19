# 本地索引选件

`examples/local_email_index.py` 只读取调用方提供的本地附件索引；`examples/local_parse_server.py` 可通过 `--index` 和 `--archive-root` 使用该索引。`GET /local/index` 查询已登记邮件及附件，`POST /local/parse` 选择明确的邮件、附件与字段配置。

## 启动与调用

在 `preprocessing` 目录安装依赖后，准备已授权的邮件归档、本地附件索引、Docling Python 和模型缓存，将以下占位路径替换为本机路径：

```powershell
uv run --no-sync python examples/local_parse_server.py `
  --archive-root '<邮件归档目录>' `
  --index '<附件索引 JSON>' `
  --docling-python '<已安装 Docling 的 Python>' `
  --model-cache '<本地模型缓存目录>' `
  --output output/local-demo --port 8765
```

服务启动后，在另一个 PowerShell 窗口列出已登记的附件，并从同一行选择邮件和附件 ID：

```powershell
$index = Invoke-RestMethod 'http://127.0.0.1:8765/local/index?limit=100'
foreach ($mail in $index.emails) {
  foreach ($attachment in $mail.attachments) {
    if ($attachment.supported) {
      [pscustomobject]@{ email_id = $mail.email_id; attachment_id = $attachment.attachment_id; filename = $attachment.filename }
    }
  }
}
$body = @{
  email_id = '<所选 email_id>'
  attachment_id = '<所选 attachment_id>'
  profile_id = 'indexed-pdf-document'
} | ConvertTo-Json -Compress
$result = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/local/parse' `
  -ContentType 'application/json' -Body $body
$result.fields | Format-Table field_name, status, value, unit
$result.materials | Format-Table filename, read_state, reason
$result.pdf_read | ConvertTo-Json -Depth 8
```

接口只选择索引中登记的具名 PDF 附件或内嵌文件；索引可用 `offset` 和 `limit` 分页。停止服务时在服务窗口按 `Ctrl+C`。若只验证接口和预处理，可不传 `--index`、Docling 路径和模型缓存，发送 `{"email_id":"missing-synthetic","profile_id":"partial-freight"}`；预期为 `partial`，缺失附件标记为受阻。

合成样例可用 `uv run --no-sync python examples/local_parse_server.py --archive-root . --output output/local-parse` 启动。当前命令行以 `--index` 选择授权材料，不支持旧文档中的 `--sample-registry` 参数；请求本身不接收文件路径。

## 字段配置

读取前核对索引中的来源和内容身份；索引不代表完整邮箱，也不会主动扫描邮箱或下载远程附件。真实索引、归档、邮件内容及其标识应保留在受控本机，不提交到仓库。

索引邮件正文支持 `local-current-body-totals` 和 `local-current-body-inquiry`；前者请求包装数量、总毛重、总体积和箱数，后者进一步请求有明确来源的询盘字段。`local-current-body-inquiry-locations` 区分货源地、提货城市和起运港／机场；`local-current-body-inquiry-weights` 区分整票净重、毛重、计费重，以及单件净重、毛重。正文请求只传 `email_id` 和 `profile_id`。响应可包含 `email_metadata`，邮件头姓名和发送时间不自动等同业务联系人或询盘创建时间。

PDF 货物扩展配置可返回商品行、单据观察和尺寸观察；新增观察来自已校验的读取产物，字段范围和来源分别保留。基础及扩展文档配置共享表格总计能力；复杂版式、扫描件和不明确的包装或重量口径仍需人工核对。

此接口会在读取前检查非安全数据表 PDF：明确的纯扫描件使用 OCR 首读，装箱单名称可触发表格首读，其余沿原生读取；探测失败或不明确不会被当成纯扫描件。混合页、未读页及字段来源仍需逐项检查。

`local-attachment-weight-table` 用于选定 PDF 表格的重量核对。先从 READ 来源确认表格指针和页码；货行还需提供一基行号及原文中唯一的行标记。请求示例：

```json
{
  "email_id": "<已选邮件 ID>",
  "attachment_id": "<已选附件 ID>",
  "profile_id": "local-attachment-weight-table",
  "weight_table": {
    "page": 1,
    "table": "/parts/0/document/tables/0",
    "row": 2,
    "row_label": "<原文货行标记>"
  }
}
```

省略 `row` 和 `row_label` 时核对整票总计；表格指针须来自本次 READ，不能直接套用示例。响应保留 `quantity_scope`、`quantity_kind` 和来源。索引 HTTP 入口只接收 PDF 页选择；Excel 表格需通过本地 Python 接口显式绑定工作表。

启动参数 `--pdf-ocr-page <正整数>` 可对所选 PDF 的指定页补 OCR；需提前配置本地 Docling 环境与模型缓存，仍需检查未读页及来源。`--extraction-agent deepseek` 会为符合条件的请求显式启用外部候选提取，要求通过 `DEEPSEEK_API_KEY` 或私有配置提供密钥；没有该参数时不调用外部模型。旧的 `minimax` CLI 参数已由 `deepseek` 取代。

如需对当前正文引用的内嵌图片补充本地 OCR，可显式添加 `--inline-image-ocr`；默认关闭。公司候选需通过发件域名、正文 CID、图片读取与来源校验，成功时会生成可独立回读的新材料结果版本；读取失败或证据不足时保留原结果。
