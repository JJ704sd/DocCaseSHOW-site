# 询盘本地结构化结果

工作台 `group_review` 在规则及 Agent 结果合并后调用 `doccase_preprocessing.inquiry_runtime.build_inquiry_result`。组级补提和接受字段变化后再次读取组结果时重新组装；工作台展示、下载按钮及 `/api/group-result` 均使用同一份 `doccase-local-inquiry/1` 数据。原 `group_fields`、材料交接和正式业务投影不变。

首询不是参考邮件。工作台可显式选择“已核实首询”；本地 API 可在原 `current`、`email` 参数外增加 `first_inquiry=<email_ref_id>`，必须是所选组成员。未传时首询邮件头、直接字段和首询货票计算留空；逐封邮件头和各自的补充结果仍可查看。该选择属于当前工作台会话，不写回原始邮件。

结果中的 `emails` 按邮件列出 F–I；`first_inquiry` 保存已确认首询及其字段；`shipment` 保存 AA、AB；`cargo_lines` 每行保存 AL 和原有货行信息；`supplements` 是独立材料范围；`business_system` 是 N–P。计算字段的 `inputs` 和 `sources` 保留对应值的来源和对象 ID，表格结果还保留工作表或 PDF 页、表、行和单元格指针。没有绑定成功或部分成功的 READ 时，预览字段不会参与货票计算。

正文中可定位的货票和货行分别保存在 `shipments` 与 `cargo_lines`，带稳定对象标识、位置和来源角色。历史转引保持历史角色；回复只有在同票关系经核验时才作为补充。Markdown 缺少对应内容时，可使用同次验证通过的 READ。

Excel 货行体积可按工作簿显示格式展示；当原始单元格来源与展示值能够验证一致时，比重使用原始精度。正文内嵌签名图的补充字段仅来自已核对内容摘要的当前图片读取结果，需匹配邮件发件地址；历史引用和隐藏图片不参与。

业务系统接入点为 `Workbench(..., business_record_provider=provider)`，也可直接向 `build_inquiry_result` 传入 `system_result`。未设置 provider 时三个字段为 `not_connected`。provider 接收 `(inquiry_id, email_ref_ids)`，该 `inquiry_id` 是当前选择邮件生成的本地组标识；调用方负责将其与权威询盘唯一匹配并校验，再返回：

```python
{
    "state": "ok",
    "matches": [{
        "verified": True,
        "group_id": inquiry_id,
        "inquiry_id": "verified-system-inquiry-id",
        "source": {"kind": "business_system", "ref": "traceable-record-reference"},
        "fields": {
            "salesperson": "...",
            "inquiry_created_at": "...",
            "internal_reference": "...",
        },
    }],
}
```

`matches` 为空或多条分别输出 `no_match`、`multiple_matches`；查询失败返回 `{"state": "query_failed"}` 或抛出异常；不满足来源、组身份或已核验条件为 `match_unverified`。缺失字段是 `missing_in_record`。邮件发件人、发送时间和正文编号不自动代填系统字段。

当前仓库没有上述权威接口的可用连接和正式接口的已批准字段映射。`doccase-local-inquiry/1` 是本地工作台结构，不是新增对外契约；正式投影沿用既有业务确认和版本要求。

现有 V1 草案投影虽然列出询盘 `density`、`is_bulk` 和需求 `density`，但缺少本次字段到这些位置的已批准对象范围和单位映射；货行比重、逐封邮件头和三个业务系统值也没有对应的已批准投影。草案叶子仅接受 `known`／`missing`，无法直接承载 `boundary_pending`、冲突及完整来源链，因此本轮不将本地结果塞进草案接口。

历史独立脚本 `local_group_compare.compare_selected` 保留原有比较语义，1000 kg 按 `>=1000` 计算；它不输出工作台新的 `doccase-local-inquiry/1` 结果。业务表的阈值确认前，不应把该旧脚本的边界结果与本结果混用。
