"use strict";

const routeLabels = {preview:"快速预览", docling:"Docling · 本地规则", deepseek:"Docling · DeepSeek Agent",
  "deepseek-group":"Docling · DeepSeek（业务组）"};
const pipelineStates = {ready:"已交接", success:"已完成", partial_success:"部分完成", partial:"部分确定",
  failure:"失败", failed:"失败", blocked:"受阻", skipped:"已跳过", not_started:"未开始", complete:"已完成"};
const pipelineReasons = {
  markdown_unavailable:"没有可展示的 Markdown。", markdown_export_failed:"Markdown 导出失败，保留读取与字段结果。",
  markdown_provider_missing:"没有可用的 Markdown，使用同一材料的已有 READ。", pipeline_failed:"链路未能完成，仍可核对原文与已有预览。",
  source_scope_unconfirmed:"片段范围未确认", image_placeholder_not_text:"图片占位，未作为文字", read_table_not_exported:"READ 表格未导出",
  read_content_not_exported:"READ 内容未导出", markdown_read_mapping_unconfirmed:"Markdown 与 READ 位置未能对应",
  material_or_object_not_eligible:"该材料或对象不符合 Agent 条件", candidate_or_evidence_invalid:"候选或证据未通过校验",
  no_eligible_markdown_evidence:"没有符合条件的 Markdown 证据", no_eligible_read_evidence:"没有符合条件的 READ 证据",
  evidence_not_supported:"证据不支持该字段、数值或业务口径",
  evidence_binding_failed:"引用的片段无法定位到该材料",
  invalid_candidate_value:"候选值或单位格式不符合字段要求",
  field_scope_mismatch:"证据的字段口径、货票或行列范围不匹配",
  conditional_or_alternative:"原文仍有条件或多个备选，无法定值",
  invalid_response_shape:"候选格式未通过校验", invalid_json:"模型响应无法读取",
  invalid_shape:"模型响应格式不符", invalid_candidate:"候选格式不符",
  invalid_candidate_or_evidence:"候选或引用位置无效",
  role_or_shipment_scope_mismatch:"证据的邮件角色或货票归属不一致",
  local_proof_missing:"同一份原文无法证明该值及口径",
  conflicting_verified_values:"同一字段有多个经核验的不同值",
  already_established:"已有结果，保留首轮值",
  role_conflict:"历史转引或来源角色不适合补提",
  no_selected_evidence:"未找到能证明该对象缺项的证据",
  no_approved_fragment:"没有符合出站范围的证据片段",
  request_limit:"本组请求额度已用完",
  timeout_or_service_failure:"请求超时或模型服务未完成",
  object_or_level_mismatch:"货票、货行或字段层级不匹配",
  semantic_or_unit_mismatch:"字段含义或单位不符",
  established_value_conflict:"候选与已有已确认值冲突",
  first_inquiry_unconfirmed:"首询未确认", first_inquiry_body_missing:"首询正文不可用",
  no_bound_evidence:"没有能回查到本材料的证据",
  read_unavailable:"材料没有可复用的读取结果",
  timeout:"模型请求超时", service_error:"模型服务未完成请求",
  attachment_range_limited:"读取范围受限", attachment_no_read_text:"当前材料没有读到文字或单元格",
  attachment_source_unverified:"字段证据无法对应到已读取的原材料位置。",
  attachment_source_not_indexed:"已读取但未对应到 Markdown 的位置",
};

function pipelineReason(code) {return pipelineReasons[code] || reasons[code] || code || "";}

function renderParsedMarkdown(content, markdown, {compact = false} = {}) {
  const panel = node(compact ? "details" : "section", "reading parsed-markdown");
  if (compact) panel.open = true;
  panel.append(node(compact ? "summary" : "h3", "", compact ? "Markdown（整份材料）" : "Markdown"),
    node("pre", compact ? "raw compact-markdown" : "raw",
      markdown.text.replace(/\r?\n(?:[^\S\r\n]*\r?\n){2,}/g, "\n\n")));
  if (markdown.truncated) panel.append(node("p", "compare-help", "仅显示前 180,000 个字符。"));
  content.append(panel);
}
function supportsPipeline(row) {return ["pdf","xlsx","html","text","png","jpeg"].includes(row.format);}

function updateRouteNote() {
  const note = $("route-note");
  const selector = $("parse-route"), route = selector.value;
  note.textContent = route === "deepseek-group"
    ? "导入时在本机读取。点击业务组补提后，才向 DeepSeek 发送符合条件的证据。" +
      (selector.dataset.groupContext === "true" ? "首轮仍缺值时，可追加一次同组脱敏证据核对。" : "")
    : route === "docling" ? "正文和受支持的附件分别生成 Markdown，不调用外部模型。" : "沿用原有原文预览和字段提取。";
}

async function loadEvaluation() {
  const target = $("evaluation-content");
  if (target.dataset.loaded) return;
  target.dataset.loaded = "loading";
  target.replaceChildren(empty("正在读取本地评测存档…"));
  try {
    const {cohort, confirmed, timing, fields} = await request("/api/evaluation/latest");
    const s = timing.seconds_per_email, cards = node("dl", "evaluation-metrics");
    const metric = (title, value, note) => {
      const card = node("div");
      card.append(node("dt", "", title), node("dd", "", value), node("p", "compare-help", note));
      cards.append(card);
    };
    metric("字段提取准确率（Precision）", "整体待定", `已判定 ${confirmed.hits} 项正确、${confirmed.wrong} 项确认误填；仍有输出待核。`);
    metric("字段召回率（Recall）", "整体待定", `${confirmed.hits} / ${confirmed.reviewed} 为非空精标的确认命中覆盖。缺材料、缺首询和待核口径未判为漏提。`);
    metric("整封邮件解析通过率", "本轮未测", "需按固定关键字段清单逐封验收正文和附件。业务组回归不能换算为整封通过率。");
    metric("原始邮件平均处理耗时", `${s.mean.toFixed(3)} 秒／封`,
      `历史端到端实测：P50 ${s.p50.toFixed(3)} 秒，P95 ${s.p95.toFixed(3)} 秒。本轮 ${timing.replay_batch_seconds.toFixed(3)} 秒为整批离线回放。`);
    target.replaceChildren(node("p", "compare-help", `已保存的固定 READ 评测 · ${cohort.groups} 组 / ${cohort.emails} 封邮件 / ${cohort.reads} 份 READ / ${cohort.fields} 个字段。评测范围以本地存档记录为准。打开本页不会重跑模型。`),
      node("p", "evaluation-result", `确认命中 ${confirmed.before} → ${confirmed.hits}：首询 ${confirmed.primary}、补充 ${confirmed.supplements}、货行 ${confirmed.cargo_rows}。`), cards);
    const coverage = node("details", "evaluation-section");
    coverage.append(node("summary", "", `${cohort.fields} 字段的命中与对照数`),
      node("p", "compare-help", `${confirmed.missing_original} 个货行位置在本批未见可用目标格式原件；仍有 ${confirmed.still_missing_primary} 项首询字段未补回。`),
      tableOf(["范围", "列", "字段", "确认命中", "非空对照"], fields.map((f) => [
        f.scope === "group" ? "组级" : "货行", f.column, f.label, f.hits, f.reviewed,
      ])));
    const chain = node("details", "evaluation-section");
    const stages = node("ol", "pipeline-steps");
    for (const [name, description] of [
      ["预处理与读取", "从 EML 拆出正文和附件，按材料读取；必要时 OCR，保存 READ。"],
      ["导出与取证", "导出 Markdown 和来源索引，回指正文、PDF 页表或 Excel 单元格。"],
      ["首轮提取与校验", "本地规则先取明确值；显式启用后逐材料用 Agent 提候选，再核对值、单位和归属。"],
      ["结构化输出与可选补提", "输出首询或当前字段、补充、货行与待核。启用组级补提后，采纳结果只进补充栏。"],
    ]) {
      const stage = node("li", "pipeline-step");
      stage.append(node("strong", "", name), node("p", "compare-help", description));
      stages.append(stage);
    }
    chain.append(node("summary", "", "当前实现链路"), stages);
    target.append(coverage, chain);
    target.dataset.loaded = "yes";
  } catch {
    target.replaceChildren(alert("本机没有可回读的评测存档，或存档校验未通过。"));
    delete target.dataset.loaded;
  }
}

function pipelineStages(content, chain) {
  const prep = chain.preprocessing, read = chain.read, md = chain.markdown, agent = chain.agent;
  const steps = node("ol", "pipeline-steps");
  const accepted = agent.events.filter((e) => e.outcome === "accepted").length;
  const stage = (title, state, description) => {
    const entry = node("li", "pipeline-step");
    entry.append(node("strong", "", title), node("span", "pipeline-step-state", state));
    if (description) entry.append(node("p", "compare-help", description));
    steps.append(entry);
  };
  stage("预处理", pipelineStates[prep.state] || prep.state, `${prep.filename} · ${prep.format.toUpperCase()}`);
  stage("Docling → Markdown", pipelineStates[md.state] || md.state,
    md.fragment_count != null ? `${md.fragment_count} 个可定位片段 · ${read.reader || "Docling"}` : pipelineReason(md.reason));
  stage("Agent 提取", !agent.enabled ? "本地规则" : accepted ? `${accepted} 个候选已采纳` : agent.adapter_requested ? "未采纳候选" : "未触发",
    agent.enabled ? `${agent.model_name || agent.model || "解析 Agent"} · 候选按原文证据校验` : "没有请求外部模型");
  stage("结构化输出", pipelineStates[chain.structured.state] || chain.structured.state,
    `${chain.structured.fields.filter((f) => f.status === "known").length} / ${chain.structured.fields.length} 项有值`);
  content.append(steps);
}

function renderPipeline(content, chain, {openSource} = {}) {
  const {preprocessing:prep, read, markdown:md, agent, structured} = chain;
  pipelineStages(content, chain);
  if (agent.events.length) {
    const eventPanel = node("section", "reading pipeline-fields");
    eventPanel.append(node("h3", "", "Agent 候选记录"));
    const outcomes = {accepted:"已采纳", rejected:"已拒绝", skipped:"未触发", abstained:"弃答", failed:"失败", fallback:"未采纳，保留规则结果"};
    for (const event of agent.events) {
      const name = event.requirement_id?.replace(/^REQ-/, "").toLowerCase();
      const evidence = event.representation === "read" ? "同材料 READ 回退" : event.representation === "markdown" ? "Markdown 证据" : "";
      eventPanel.append(node("p", "compare-help", [names[name] || event.requirement_id, outcomes[event.outcome] || event.outcome,
        evidence, pipelineReason(event.read_fallback_reason), pipelineReason(event.reason)].filter(Boolean).join(" · ")));
    }
    content.append(eventPanel);
  }
  const panel = node("section", "reading pipeline-fields");
  panel.append(node("h3", "", "经校验的结构化字段"), node("p", "compare-help", "按当前材料列出，未确定或冲突保留原状态。"));
  const known = structured.fields.filter((f) => f.status === "known");
  const rest = structured.fields.filter((f) => f.status !== "known");
  const renderField = (field) => {
    const row = node("article", "field"), head = node("div", "field-top");
    const status = node("div", "compare-actions");
    status.append(badge(field.status));
    if (field.via) status.append(node("span", "", field.via === "agent" ? "Agent 候选 · 已校验" : "规则"));
    head.append(node("strong", "field-name", names[field.name] || field.name), status);
    row.append(head, node("p", "field-value", resultText(field.value, field.unit || (field.name === "package_count" ? "package" : null))));
    if (field.reason) row.append(node("p", "field-reason", pipelineReason(field.reason)));
    for (const source of field.sources) {
      const evidence = node("div", "pipeline-evidence");
      if (openSource && (source.page || source.sheet || source.line || source.table)) evidence.append(sourceButton(source, () => openSource(source)));
      else evidence.append(node("span", "", source.excerpt));
      if (source.markdown_evidence?.length) evidence.append(node("code", "", source.markdown_evidence.join(" / ")));
      if (source.pointer) evidence.append(node("code", "", `${source.page ? `第 ${source.page} 页 · ` : ""}${source.pointer}`));
      row.append(evidence);
    }
    return row;
  };
  if (!known.length) panel.append(empty("此材料尚无可确定的字段。"));
  known.forEach((field) => panel.append(renderField(field)));
  if (rest.length) {
    const pending = node("details"); pending.open = rest.some((f) => f.status === "conflict");
    pending.append(node("summary", "", `待核对字段（${rest.length}）`), ...rest.map(renderField));
    panel.append(pending);
  }
  content.append(panel);
  const mdPanel = node("details", "pipeline-markdown"); mdPanel.open = true;
  mdPanel.append(node("summary", "", "Docling Markdown"));
  if (md.text != null) mdPanel.append(node("pre", "pipeline-code", md.text || "没有导出文字。"));
  else mdPanel.append(node("p", "compare-help", pipelineReason(md.reason)));
  if (md.truncated) mdPanel.append(node("p", "compare-help", "预览只显示前 180,000 个字符。"));
  for (const [code,count] of Object.entries(md.limitations || {})) mdPanel.append(node("p", "compare-help", `${pipelineReason(code)}：${count}`));
  if (read.unread_pages?.length) mdPanel.append(alert(`未读页：${read.unread_pages.join("、")}`));
  content.append(mdPanel);
  const audit = node("details", "pipeline-audit");
  audit.append(node("summary", "", "材料、候选与结构化 JSON"), node("pre", "pipeline-code",
    JSON.stringify({preprocessing:prep, read, agent, structured}, null, 2)));
  content.append(audit);
}

function renderPipelineMaterial(content, job, row, data, openSource) {
  if (data?.chain) return renderPipeline(content, data.chain, {openSource});
  if (data?.chain_error) return content.append(alert(pipelineReason(data.chain_error)));
  if (job.route === "preview") return content.append(empty("本轮选用快速预览。重新导入时可选择 Docling 路线。"));
  if (!supportsPipeline(row)) return content.append(empty("该材料仍可在常规预览中核对。"));
  if (["queued", "running"].includes(row.state)) return content.append(empty("预处理已登记材料，正在等待读取、导出或字段校验。"));
  content.append(empty(reason(row.reason) || "没有可展示的链路结果。"));
}

function renderPipelineIndex(content, job) {
  const box = node("section", "reading pipeline-fields");
  box.append(node("h3", "", routeLabels[job.route] || routeLabels.preview), node("p", "compare-help", "选择正文或附件查看该材料的 Markdown、候选和字段。整封邮件核对仍可查看原文与附件。"));
  if (!job.materials.length) box.append(empty(reason(job.reason) || "正在预处理邮件。"));
  for (const row of job.materials) {
    const line = node("div", "pipeline-material");
    line.append(button(materialName(row), () => select(job.id, row.id), "control"), badge(row.state));
    if (!supportsPipeline(row)) line.append(node("span", "compare-help", "常规预览"));
    box.append(line);
  }
  content.append(box);
}

function renderBodyReadEvidence(content, chain) {
  const evidence = new Map();
  for (const field of chain.structured.fields) for (const source of field.sources) {
    if (!source.pointer) continue;
    const key = `${source.pointer}/${source.start}/${source.end}`;
    if (!evidence.has(key)) evidence.set(key, {text:source.excerpt, pointer:source.pointer});
  }
  if (evidence.size) content.append(node("h3", "", "Docling 字段来源"), renderLines([...evidence.values()]));
}
