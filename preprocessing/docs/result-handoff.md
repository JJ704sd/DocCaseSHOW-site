# 本地材料结果与交接

主解析结果包含字段状态、值、单位及来源绑定。`output_consume.py` 检查输入范围和来源，`output_snapshot.py` 将材料结果按版本保存并回读；`output_revision.py` 和 `output_reprocess.py` 支持有证据的修正与材料变更后的重新处理。

`complete` 只表示调用方声明的读取与字段范围已完成，不代表原件完整、字段准确或业务验收通过。未读、冲突和来源不足的字段仍需保留状态；业务决策与正式接口映射由接入方另行确认。

`output_project.py` 仅提供本地合成草案演示：示例接口标识为 `synthetic-interface-document`，版本为 `1`。它不包含任何实际业务接口文档标识，也不能作为正式映射或生产接口使用。

在 `preprocessing` 目录可运行相关合成测试：

```powershell
uv run --no-sync pytest -q tests/test_output_snapshot.py tests/test_output_revision.py tests/test_output_project.py
```
