"use strict";

const $ = (id) => document.getElementById(id);
const node = (tag, className, value) => {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (value != null) el.textContent = value;
  return el;
};
const button = (label, fn, className) => {
  const el = node("button", className, label);
  el.type = "button";
  el.addEventListener("click", fn);
  return el;
};
const states = { queued: "等待", running: "解析中", ready: "已读取", partial: "部分读取",
  failed: "失败", unsupported: "暂不支持", known: "有来源", uncertain: "未确定",
  conflict: "有冲突", missing: "缺失", not_applicable: "不适用", skipped: "已跳过",
  match: "一致", different: "不一致", unresolved: "待核对" };
const reasons = {
  no_evidence: "当前材料未找到可支持该字段的内容。", multiple_values: "读到了不同的值，需要人工核对。",
  field_not_found_or_scope_unread: "当前材料未找到可支持该字段的内容，或相关范围未读。",
  explicitly_referenced_material_unread: "字段引用的材料未读取。", no_materials: "没有可读取的正文或附件。",
  intake_failed: "无法读取文件内容，请检查文件是否完整。", unsupported_format: "首版暂不支持此格式。",
  unsupported: "首版暂不解析嵌套邮件或此类附件。", conversion_required: "请另存为 XLSX 后重试。",
  corrupt: "文件已损坏或不完整。", format_mismatch: "文件内容与扩展名或声明格式不符。",
  encrypted: "文件已加密，请先解密后重试。", invalid_encoding: "请将文本另存为 UTF-8 后重试。",
  missing_content: "没有可读取的文件内容。", unavailable: "没有可读取的文件内容。",
  read_failed: "此材料没有解析成功。", reader_missing: "未找到本机 Docling Python 环境。",
  ocr_models_missing: "本机缺少 OCR 模型文件。", ocr_empty: "未识别到文字，可以打开原图核对。",
  timeout: "解析超时。", limit_exceeded: "材料超过本机解析限制。",
  file_too_large: "文件超过 20 MiB。", invalid_file: "文件为空或文件名不可用。",
  queue_full: "解析队列已满，等一批完成后再添加。", upload_timeout: "读取文件超时，请重试。",
  native_pass_failed: "部分 PDF 原生内容未能读取。", ocr_pass_failed: "部分页面未能完成 OCR。",
  structure_pass_failed: "部分页面未能识别表格结构。", network: "无法连接本机解析服务。",
  skipped_by_scope: "按本轮解析选项跳过。", no_selected_materials: "没有符合本轮解析选项的材料。",
  invalid_parse_scope: "不支持此解析选项。", comparison_not_email: "只有整封邮件可做正文与附件核对。",
  comparison_invalid_selection: "请选择这封邮件中可核对的附件。",
  group_invalid_selection: "请选择 2–12 封 EML，并从中指定一封当前邮件。",
  pipeline_route_unavailable: "此解析路线未启用，请切换到本地规则或快速预览。",
  ambiguous_body: "有多个当前正文，不能确认使用哪一份。", no_current_body: "这封邮件没有可用的当前正文。",
};
const names = {
  package_count:"包装件数", ctns:"纸箱箱数", line_gross_weight:"毛重", line_net_weight:"净重",
  unit_net_weight:"单件净重", unit_gross_weight:"单件毛重", line_volume:"体积", product_name:"商品品名",
  shipment_net_weight:"整票净重", shipment_gross_weight:"整票毛重", shipment_chargeable_weight:"整票计费重",
  line_chargeable_weight:"货行计费重",
  package_type:"包装类型", raw_observations:"原文尺寸或重量观察", length_cm:"长度", width_cm:"宽度", height_cm:"高度",
  destination:"目的地", origin:"旧版起运字段", cargo_origin:"货源地", pickup_city:"提货城市", departure_port:"起运港／机场",
  trade_terms:"贸易条款", cargo_ready_time:"货好时间", customer_company:"客户公司",
  contact_name:"联系人", contact_phone:"联系电话", signature_email:"签名邮箱", salesperson:"业务员",
  internal_reference:"内部参考号", cargo_type:"货物类型", cargo_description:"货物描述", business_remarks:"业务备注",
  chargeable_weight:"计费重量", density_kg_per_m3:"比重", is_large_cargo:"大货", port_of_loading:"装运港", item_quantity:"商品总件数",
};
const units = {carton: "箱", package: "件", piece: "件", set: "套"};
const jobs = new Map();
const detailCache = new Map();
const detailRequests = new Map();
const comparisons = new Map();
const seen = new Set();
let activeJob = null, activeItem = null, detail = null, view = "original", filter = "all";
const newPosition = () => ({page: 1, sheetIndex: 0});
let materialPosition = newPosition(), poll = null, selectionVersion = 0;
let roundVersion = 0, replaceOnPick = false;
const compareNames = ["package_count", "line_gross_weight", "line_volume"];
const inquiryPriority = ["cargo_origin", "pickup_city", "departure_port", "destination", "trade_terms",
  "package_count", "shipment_gross_weight", "shipment_net_weight", "shipment_chargeable_weight",
  "line_gross_weight", "line_volume"];
const scopeNames = {all: "正文和附件", body: "仅正文", attachments: "仅附件"};

const badge = (state) => node("span", `state ${state}`, states[state] || state);
const empty = (text) => node("p", "empty-state", text);
const alert = (text) => node("p", "alert", text);
const reason = (code) => reasons[code] || (code ? `请核对材料或字段来源（${code}）。` : "");
const keyOf = (job, item) => `${job}/${item}`;
const itemURL = (job, item) => `/api/jobs/${job}/materials/${item}`;
const materialName = (item) => item.kind === "body" ? "邮件正文" : item.filename;
const isAttachment = (item) => ["attachment", "inline"].includes(item.kind);
const hasDetail = (item) => ["ready", "partial"].includes(item.state) || item.known !== undefined;
const isComparing = (id) => (activeJob === id && activeItem === null && view === "comparison")
  || (view === "group" && groupReview.reading === id);
const resultText = (value, unit) => value == null ? "—" : `${typeof value === "object" ? JSON.stringify(value) : value}${unit ? ` ${units[unit] || unit}` : ""}`;

async function request(path, init) {
  const response = await fetch(path, init);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "network");
  return data;
}

async function checkConnection() {
  const indicator = $("connection");
  let connected = false, deepseek = false, healthContext = false;
  try {
    const health = await request("/api/health", {signal: AbortSignal.timeout(4000)});
    connected = health.status === "ready";
    deepseek = connected && health.deepseek === true;
    healthContext = deepseek && health.group_context === true;
  } catch { /* The indicator and upload notice show the connection state. */ }
  indicator.dataset.status = connected ? "ready" : "offline";
  indicator.lastElementChild.textContent = connected ? "本机接口已连接" : "本机接口未连接";
  const route = $("parse-route");
  route.querySelector('[value="deepseek-group"]').disabled = !deepseek;
  if (!deepseek && route.value === "deepseek-group") route.value = "docling";
  route.dataset.groupContext = String(connected && healthContext);
  updateRouteNote();
}

async function enqueue(files, {demo = null} = {}) {
  const round = roundVersion;
  const scope = demo ? "all" : $("parse-scope").value, route = demo ? "docling" : $("parse-route").value;
  const messages = [], emailIds = [];
  $("notice").hidden = true;
  for (const file of files) {
    if (round !== roundVersion) return;
    try {
      const isEmail = /\.eml$/i.test(file.name);
      const selectedRoute = route === "deepseek-group" && !isEmail ? "docling" : route;
      const { id, metadata } = await request("/api/jobs", {method: "POST", body: file,
        headers: {"X-DocCase-File": encodeURIComponent(file.name), "Content-Type": "application/octet-stream",
          "X-DocCase-Parse-Scope": scope, "X-DocCase-Route": selectedRoute}});
      if (round !== roundVersion) return;
      jobs.set(id, {id, filename: file.name, is_email: isEmail, parse_scope: scope,
        route:selectedRoute, state: "queued", materials: [], metadata:metadata || []});
      if (isEmail && demo) registerDemoMail(id, demo, emailIds[0] || id, file.name);
      else if (isEmail && file.webkitRelativePath) registerGroupFile(id, file.webkitRelativePath);
      if (isEmail) emailIds.push(id);
      else if (!activeJob && !emailIds.length) select(id, null);
      renderQueue();
      if (view === "group") syncGroup();
      startPolling();
    } catch (error) {
      messages.push(`${file.name}：${reasons[error.message] || reasons.network}`);
    }
  }
  if (round !== roundVersion) return;
  if (emailIds.length) showImportedGroups(emailIds);
  if (messages.length) {
    $("notice").textContent = messages.join("　");
    $("notice").hidden = false;
    checkConnection();
  }
  return emailIds;
}

function startPolling() {
  if (poll === null) poll = setTimeout(refresh, 300);
}

async function refresh() {
  const round = roundVersion;
  const pending = Array.from(jobs.values()).filter((job) => ["queued", "running"].includes(job.state));
  const updated = await Promise.allSettled(pending.map((job) => request(`/api/jobs/${job.id}`)));
  if (round !== roundVersion) return;
  updated.forEach((entry, index) => {
    if (entry.status === "fulfilled") jobs.set(entry.value.id, entry.value);
    else if (entry.reason.message === "not_found") {
      const job = pending[index];
      jobs.set(job.id, {...job, state: "failed", reason: "network"});
    }
  });
  const job = jobs.get(activeJob);
  const row = job?.materials.find((item) => item.id === activeItem);
  if (view === "group") syncGroup();
  else if (job?.materials.length && activeItem === null && !seen.has(activeJob)) {
    if (job.is_email) select(activeJob, null, true);
    else select(activeJob, job.materials[0].id);
  } else if (row || job) {
    const stateKey = JSON.stringify([activeJob, row || [job.state, job.reason, job.metadata, job.materials]]);
    if (stateKey !== refresh.previous) {
      refresh.previous = stateKey;
      renderReader();
      loadDetail();
    }
  }
  renderQueue();
  poll = Array.from(jobs.values()).some((job) => ["queued", "running"].includes(job.state))
    ? setTimeout(refresh, 650) : null;
}

function select(job, item, overview = false) {
  if (overview) seen.add(job);
  activeJob = job;
  activeItem = item;
  selectionVersion += 1;
  detail = detailCache.get(keyOf(job, item)) || null;
  const selected = jobs.get(job);
  view = item === null && selected?.is_email ? "comparison" : "original";
  filter = "all"; materialPosition = newPosition();
  renderQueue(); renderReader(); renderFields();
  return loadDetail();
}

async function loadDetail() {
  const row = jobs.get(activeJob)?.materials.find((item) => item.id === activeItem);
  if (!row || detail || !hasDetail(row)) return;
  const version = selectionVersion;
  try {
    const data = await fetchDetail(activeJob, activeItem);
    if (version !== selectionVersion) return;
    detail = data;
    renderReader(); renderFields();
  } catch {
    if (version === selectionVersion) $("content").append(alert("无法加载此材料的解析结果。"));
  }
}

function fetchDetail(job, item) {
  const key = keyOf(job, item), round = roundVersion;
  if (detailCache.has(key)) return Promise.resolve(detailCache.get(key));
  if (!detailRequests.has(key)) {
    detailRequests.set(key, request(itemURL(job, item)).then((data) => {
      if (round === roundVersion) detailCache.set(key, data);
      return data;
    }).finally(() => detailRequests.delete(key)));
  }
  return detailRequests.get(key);
}

function renderQueue() {
  const hasJobs = jobs.size > 0;
  $("workspace").hidden = !hasJobs;
  $("empty").hidden = hasJobs;
  $("new-pick").hidden = !hasJobs;
  $("group-open").disabled = !Array.from(jobs.values()).some((job) => job.is_email);
  $("group-open").setAttribute("aria-pressed", String(view === "group"));
  $("group-open").title = $("group-open").disabled ? "先导入 EML" : "查看业务组字段和组内邮件";
  $("pick").textContent = hasJobs ? "添加文件" : "选择文件";
  document.body.classList.toggle("has-jobs", hasJobs);
  $("file-count").textContent = `${jobs.size} 个文件`;
  $("queue-title").textContent = view === "group" ? "业务组" : "材料";
  const list = $("jobs"), scroll = list.scrollTop;
  list.setAttribute("aria-label", view === "group" ? "业务组列表" : "文件和附件列表");
  const focused = list.contains(document.activeElement) ? document.activeElement.dataset.row : null;
  list.replaceChildren();
  if (view === "group") {
    renderGroupSidebar(list);
    list.scrollTop = scroll;
    if (focused) Array.from(list.querySelectorAll("button")).find((el) => el.dataset.row === focused)?.focus({preventScroll:true});
    return;
  }
  for (const job of jobs.values()) {
    const group = node("div", "job");
    const head = button("", () => select(job.id, null, true), "job-title");
    head.dataset.row = job.id;
    head.setAttribute("aria-current", String(view !== "group" && job.id === activeJob && activeItem === null));
    head.append(node("span", "", job.filename), badge(job.state));
    group.append(head);
    for (const item of job.materials) {
      const row = button("", () => isComparing(job.id) && isAttachment(item)
        ? openAttachment(job, item.id) : select(job.id, item.id), "material");
      row.dataset.row = keyOf(job.id, item.id);
      row.setAttribute("aria-current", String(view !== "group" && job.id === activeJob && item.id === activeItem));
      row.setAttribute("aria-label", `${materialName(item)}，${states[item.state]}`);
      const fmt = item.format === "text" ? "TXT" : item.format === "jpeg" ? "JPG" : item.format.toUpperCase().slice(0, 5);
      row.append(node("span", "file-type", fmt), node("span", "", materialName(item)), node("span", `dot ${item.state}`));
      group.append(row);
    }
    list.append(group);
  }
  list.scrollTop = scroll;
  if (focused) Array.from(list.querySelectorAll("button")).find((el) => el.dataset.row === focused)?.focus({preventScroll: true});
}

function tab(label, value) {
  const control = button(label, () => {view = value; renderReader(); $("tabs").querySelector('[aria-pressed="true"]')?.focus();});
  control.setAttribute("aria-pressed", String(view === value));
  return control;
}

function renderReader() {
  if (view === "group") return renderGroupReader();
  const job = jobs.get(activeJob);
  if (!job) return;
  const row = job.materials.find((item) => item.id === activeItem);
  const head = $("material-head"), tabs = $("tabs"), content = $("content");
  tabs.hidden = false;
  const comparing = !row && job.is_email && view === "comparison";
  const scroll = comparing && $("workspace").classList.contains("comparing") ? content.scrollTop : 0;
  const focused = comparing && content.contains(document.activeElement) ? document.activeElement.id : null;
  $("workspace").classList.toggle("comparing", comparing || view === "pipeline" || view === "chain");
  $("workspace").classList.remove("group-workspace");
  const title = node("div");
  title.append(node("h2", "", row ? materialName(row) : job.metadata?.[0]?.subject || job.filename));
  if (row) title.append(node("p", "", `${row.kind === "body" ? "当前邮件" : row.kind === "file" ? "独立文件" : "邮件附件"} / ${row.filename}`));
  else if (job.is_email) title.append(node("p", "", `解析范围：${scopeNames[job.parse_scope]} · ${routeLabels[job.route] || routeLabels.preview}`));
  head.replaceChildren(title, badge(row?.state || job.state));
  renderMailContext(job, row);
  tabs.replaceChildren();
  content.replaceChildren();
  content.scrollTop = 0;
  if (!row) {
    if (job.is_email) {
      if (job.route && job.route !== "preview") tabs.append(tab("解析链路", "pipeline"));
      tabs.append(tab("整封邮件核对", "comparison"), tab("邮件信息", "overview"));
    }
    if (view === "pipeline") renderPipelineIndex(content, job);
    else if (comparing) {
      renderComparison(content, job);
      content.scrollTop = scroll;
      if (focused) $(focused)?.focus({preventScroll:true});
    } else renderOverview(content, job);
    return;
  }
  if (job.route && job.route !== "preview" && supportsPipeline(row)) tabs.append(tab("解析链路", "chain"));
  tabs.append(tab("原文预览", "original"), tab("解析内容", "parsed"));
  renderMaterial(content, job, row, detail, {mode: view, position: materialPosition, redraw: renderReader, onSource: follow});
}

function renderMaterial(content, job, row, data, context) {
  const {mode} = context, url = itemURL(job.id, row.id);
  if (mode === "chain") return renderPipelineMaterial(content, job, row, data, context.onSource);
  if (["unsupported", "failed", "skipped"].includes(row.state)) {
    content.append(alert(row.format === "msg" ? "请将 Outlook 邮件另存为 EML 后重试。" : reason(row.reason)));
  }
  if (data?.clipped && !(mode === "original" && data.mail_preview)) {
    content.append(alert("只展示已读取的范围，超出范围的内容尚未核对。"));
  }
  if (data?.unread_pages?.length) content.append(alert(`未读到内容的页面：${data.unread_pages.join("、")}`));
  for (const warning of data?.warnings || []) content.append(alert(reason(warning)));
  if (data?.chain_error) content.append(alert(pipelineReason(data.chain_error)));
  if (mode === "parsed" && ["pdf", "xlsx"].includes(data?.type) && data.chain?.markdown?.text) {
    renderParsedMarkdown(content, data.chain.markdown, {compact:true});
  }
  if (row.format === "pdf" && row.pages) return renderPDF(content, row, url, data, context);
  if (["png", "jpeg"].includes(row.format) && mode === "original" && !["corrupt", "format_mismatch"].includes(row.reason)) {
    const image = node("img", "docpage");
    image.alt = row.filename;
    image.src = `${url}/image`;
    image.onerror = () => image.replaceWith(empty("无法预览原图。"));
    content.append(image);
    return;
  }
  if (!data) {
    if (["queued", "running"].includes(row.state)) content.append(empty("本机正在读取此材料，可先查看其他已读材料。"));
    return;
  }
  if (data.type === "xlsx") return renderSheet(content, data, context);
  if (data.type === "text" && mode === "original") {
    if (row.format === "html") {
      if (data.mail_preview) return renderOriginalMail(content, job, row, data);
      content.append(node("pre", "raw reading", data.lines.join("\n") || "没有可显示的正文文字。"));
      if (data.quoted) content.append(disclosure("历史引用", data.quoted));
      content.append(disclosure("查看原始 HTML 源码", data.original));
    } else content.append(node("pre", "raw reading", data.original));
    return;
  }
  if (data.chain?.markdown?.text) {
    renderParsedMarkdown(content, data.chain.markdown);
    const evidence = node("details", "parsed-source-lines");
    evidence.append(node("summary", "", "按行核对来源"));
    content.append(evidence);
    content = evidence;
  }
  if (data.chain && row.kind === "body") renderBodyReadEvidence(content, data.chain);
  if (data.type === "image" && data.lines.length && data.size) renderImageEvidence(content, data, `${url}/image`, row.filename);
  content.append(renderLines(data.lines));
  if (data.type === "text") {
    for (const table of data.tables || []) {
      content.append(node("h3", "", `表格 ${table.number}`), pdfTable(table));
    }
  }
  if (data.quoted) content.append(disclosure("历史引用（不参与当前正文字段提取）", data.quoted));
}

function renderMailPreviewNotes(content, {export_incomplete, replacement_characters}) {
  if (export_incomplete || replacement_characters) {
    const notes = [];
    if (export_incomplete) notes.push("这份 EML 副本标注了「部分内容未导出」。未保留的内容无法从当前副本恢复。");
    if (replacement_characters) notes.push(`保留的 HTML 正文含 ${replacement_characters} 个替代字符（�），原字符无法恢复。`);
    content.append(alert(notes.join("")));
  }
  content.append(node("p", "mail-preview-note", "按副本保留的版式展示。同封邮件内嵌图片在原位置显示，外链图片不加载。"));
}

function originalMailFrame(job, row) {
  const frame = node("iframe", "mail-original-frame");
  frame.title = "邮件 HTML 原文预览";
  frame.setAttribute("sandbox", "");
  frame.referrerPolicy = "no-referrer";
  frame.src = `${itemURL(job.id, row.id)}/original-html`;
  return frame;
}

function renderOriginalMail(content, job, row, data) {
  renderMailPreviewNotes(content, data.mail_preview);
  content.append(originalMailFrame(job, row), disclosure("查看副本 HTML 源码", data.original));
}

function renderMailContext(job, row) {
  const bar = $("mail-context");
  bar.replaceChildren();
  bar.hidden = !job.is_email || !row;
  if (bar.hidden) return;
  const back = button("返回整封邮件", () => select(job.id, null, true), "control");
  bar.append(back);
  if (groupReview.ids.has(job.id) && groupReview.result) {
    bar.append(button("返回业务组核对", openGroup, "control"));
  }
  const body = job.materials.find((item) => item.kind === "body");
  if (body) {
    const open = button("邮件正文", () => select(job.id, body.id), "control");
    open.disabled = row.id === body.id;
    bar.append(open);
  }
  const attachments = job.materials.filter(isAttachment);
  if (attachments.length) {
    const menu = node("select");
    menu.setAttribute("aria-label", "切换邮件附件");
    const prompt = new Option(`附件（${attachments.length}）`, "", false, row.kind === "body");
    prompt.disabled = true;
    menu.append(prompt);
    for (const item of attachments) menu.append(new Option(materialName(item), item.id, false, row.id === item.id));
    menu.addEventListener("change", () => {if (menu.value) select(job.id, menu.value);});
    bar.append(menu);
  }
}

function renderOverview(content, job) {
  if (job.reason) content.append(alert(reason(job.reason)));
  const email = job.metadata?.[0];
  if (email) {
    const listing = node("div", "reading");
    for (const [label, value] of [["主题", email.subject], ["发件人", email.from_addr],
      ["收件人", email.to_addrs?.join("、")], ["抄送", email.cc_addrs?.join("、")], ["时间", email.sent_at]]) {
      if (!value) continue;
      listing.append(node("h3", "", label), node("p", "", value));
    }
    content.append(listing);
  }
  if (job.materials.length) content.append(empty("从左侧选择正文或附件，核对原文和业务字段。"));
  else if (["queued", "running"].includes(job.state)) content.append(empty("正在读取文件并识别材料。"));
}

function comparisonState(id) {
  if (!comparisons.has(id)) comparisons.set(id, {ids: new Set(), result: null, loading: false, error: "", revision: 0,
    inquiryFilter: "key", bodyExpanded: false, bodyParsedExpanded: false, bodySource: null, preview: null});
  return comparisons.get(id);
}

function refreshComparison(job) {
  if (!isComparing(job.id)) return;
  const review = $("mail-attachment-review"), content = $("content");
  if (!review) return;
  const scroll = content.scrollTop;
  const state = comparisonState(job.id);
  const bodyReturn = $("mail-body-source-return");
  if (bodyReturn) renderSourceReturn(bodyReturn, job, state, state.bodySource?.origin);
  review.replaceChildren();
  renderAttachmentComparison(review, jobs.get(job.id), state);
  content.scrollTop = scroll;
  return true;
}

async function compareAttachments(job, state) {
  const revision = ++state.revision;
  const query = new URLSearchParams(Array.from(state.ids, (id) => ["attachment", id]));
  state.loading = true; state.error = ""; state.result = null;
  refreshComparison(job);
  try {
    const result = await request(`/api/jobs/${job.id}/comparison?${query}`);
    if (revision === state.revision) state.result = result;
  } catch (error) {
    if (revision === state.revision) state.error = reasons[error.message] || "暂时无法核对所选附件。";
  } finally {
    if (revision === state.revision) {
      state.loading = false;
      if (refreshComparison(job)) $("comparison-result")?.focus();
    }
  }
}

function comparisonTable(label, headings, rows) {
  const wrap = node("div", "grid-wrap compare-grid"), table = node("table");
  table.setAttribute("aria-label", label);
  const head = node("thead"), header = node("tr"), body = node("tbody");
  headings.forEach((heading) => {const th = node("th", "", heading); th.scope = "col"; header.append(th);});
  head.append(header);
  for (const row of rows) {
    const line = node("tr");
    row.forEach((value, index) => {
      const cell = node(index ? "td" : "th");
      if (!index) cell.scope = "row";
      cell.append(value);
      line.append(cell);
    });
    body.append(line);
  }
  table.append(head, body); wrap.append(table);
  return wrap;
}

function compareValue(field, name) {
  const container = node("div", "compare-value");
  container.append(node("strong", "", resultText(field.value, field.unit || (name === "package_count" ? "package" : null))));
  if (field.status !== "known") container.append(badge(field.status));
  return container;
}

function compareField(job, material, name, state) {
  const field = material.fields[name], cell = node("div");
  cell.append(compareValue(field, name));
  if (field.status !== "known") cell.append(node("p", "field-reason", reason(field.reason)));
  if (field.sources.length) {
    const evidence = node("details", "compare-evidence"), links = node("div", "sources");
    evidence.append(node("summary", "", `${field.sources.length} 条来源`), links);
    for (const [index, source] of field.sources.entries()) {
      const id = `compare-source-${material.id}-${name}-${index}`;
      const origin = {id, revision: state.revision};
      const link = sourceButton(source, () => isAttachment(material)
        ? openAttachment(job, material.id, {source, origin})
        : openBodySource(job, material, source, origin));
      link.id = id;
      links.append(link);
    }
    cell.append(evidence);
  }
  return cell;
}

function renderSourceReturn(bar, job, state, origin) {
  if (origin?.section === "group") {
    bar.hidden = view !== "group";
    bar.replaceChildren(button("返回业务组字段", () => focusGroupResult(origin.id), "control"));
    return;
  }
  const inquiry = origin?.section === "inquiry";
  const valid = () => inquiry || (state.result && origin?.revision === state.revision);
  bar.hidden = !valid();
  bar.replaceChildren();
  if (bar.hidden) return;
  bar.append(button(inquiry ? "返回询盘字段" : "返回核对结果", () => {
    if (!isComparing(job.id) || !valid()) return;
    if (inquiry && !$(origin.id)) {
      state.inquiryFilter = origin.filter;
      renderInquiryOverview($("inquiry-overview"), jobs.get(job.id), state);
    }
    const link = $(origin.id);
    if (!link) return;
    const evidence = link.closest("details");
    if (evidence) evidence.open = true;
    link.scrollIntoView({block: "center", inline: "nearest"});
    link.focus({preventScroll: true});
  }, "control"));
}

function jumpTo(id) {
  const target = $(id);
  if (!target) return;
  target.scrollIntoView({block: "start"});
  target.focus({preventScroll: true});
}

function renderInquiryOverview(host, job, state) {
  host.replaceChildren();
  const heading = node("div", "mail-body-title"), title = node("div");
  title.append(node("h3", "", "询盘字段"), node("p", "compare-help", "来自当前正文。附件数据在下方单独核对。"));
  const links = node("div", "compare-actions");
  const bodies = job.materials.filter((item) => item.kind === "body");
  if (bodies.length) links.append(button("看邮件正文", () => jumpTo("mail-body-card"), "control"));
  const attachments = job.materials.filter(isAttachment).length;
  if (attachments) links.append(button(`看附件（${attachments}）`, () => jumpTo("mail-attachment-review"), "control"));
  heading.append(title, links);
  host.append(heading);
  if (bodies.length !== 1 || !hasDetail(bodies[0])) {
    const pending = ["queued", "running"].includes(job.state) && (!bodies.length || bodies.some((b) => ["queued", "running"].includes(b.state)));
    const message = pending ? "正在读取正文，结构化字段会自动显示。"
      : bodies.length === 1 ? reason(bodies[0].reason) || "当前正文没有可用的字段结果。"
      : bodies.length ? reasons.ambiguous_body : reasons.no_current_body;
    host.append(empty(message));
    return;
  }
  const [body] = bodies, slot = node("div", "inquiry-content");
  host.append(slot);
  const show = (data) => {
    if (!slot.isConnected) return;
    renderInquiryFields(slot, job, body, data, state);
  };
  const cached = detailCache.get(keyOf(job.id, body.id));
  if (cached) show(cached);
  else {
    slot.append(empty("正在加载结构化字段。"));
    fetchDetail(job.id, body.id).then(show, () => {
      if (slot.isConnected) slot.replaceChildren(alert("无法加载询盘字段。"));
    });
  }
}

function renderInquiryFields(slot, job, body, data, state) {
  slot.replaceChildren();
  const fields = data.fields, known = fields.filter((field) => field.status === "known").length;
  const conflicts = fields.filter((field) => field.status === "conflict").length;
  const stats = node("div", "inquiry-toolbar"), filters = node("div", "filters inquiry-filters");
  filters.setAttribute("aria-label", "询盘字段筛选");
  for (const [value, label] of [["key", "重点"], ["review", `待核对 ${fields.length - known}`], ["all", `全部 ${fields.length}`]]) {
    const control = button(label, () => {
      state.inquiryFilter = value;
      renderInquiryFields(slot, job, body, data, state);
      $(control.id)?.focus({preventScroll: true});
    });
    control.id = `inquiry-filter-${value}`;
    control.setAttribute("aria-pressed", String(value === state.inquiryFilter));
    filters.append(control);
  }
  stats.append(filters, node("p", "inquiry-count", `有来源 ${known} · 有冲突 ${conflicts} · 未确定 ${fields.length - known - conflicts}`));
  slot.append(stats);
  if (data.clipped) slot.append(alert("正文仅展示已读取的范围。"));
  if (data.chain_error) slot.append(alert("新链路未完成；此处展示快速预览字段。"));
  const mode = state.inquiryFilter, grid = node("div", "inquiry-grid");
  const prioritized = inquiryPriority.map((name) => fields.find((field) => field.name === name)).filter(Boolean);
  const ordered = [...prioritized, ...fields.filter((field) => !inquiryPriority.includes(field.name))];
  const visible = mode === "all" ? ordered : mode === "review"
    ? [...ordered.filter((field) => field.status === "conflict"), ...ordered.filter((field) => !["conflict", "known"].includes(field.status))]
    : ordered.filter((field) => inquiryPriority.includes(field.name) || field.status !== "uncertain");
  const evidenceLink = (source, name, index) => {
    const id = `inquiry-source-${name}-${index}`;
    const link = sourceButton(source, () => openBodySource(job, body, source, {section: "inquiry", id, filter: mode}));
    link.id = id;
    return link;
  };
  for (const field of visible) {
    const item = node("article", "inquiry-field"), head = node("div", "field-top");
    head.append(node("h4", "field-name", names[field.name] || field.name), badge(field.status));
    item.append(head);
    if (field.value != null) item.append(node("p", "inquiry-value",
      resultText(field.value, field.unit || (field.name === "package_count" ? "package" : null))));
    else if (field.status === "conflict") item.append(node("p", "field-reason", "请对照各条来源。"));
    if (field.sources.length) {
      const links = node("div", "sources");
      field.sources.forEach((source, index) => links.append(evidenceLink(source, field.name, index)));
      if (field.sources.length === 1) item.append(links);
      else {
        const more = node("details", "inquiry-evidence");
        more.append(node("summary", "", `${field.sources.length} 条来源`), links);
        item.append(more);
      }
    }
    grid.append(item);
  }
  slot.append(visible.length ? grid : empty("没有需要核对的字段。"));
  if (data.observations?.length) {
    const observations = node("details", "inquiry-observations"), links = node("div", "sources");
    const sample = data.observations.slice(0, 5);
    observations.append(node("summary", "", `原文待归类（${data.observations.length} 条）`),
      node("p", "compare-help", "这些表述的包装或重量口径还需确认。"), links);
    sample.forEach((source, index) => links.append(evidenceLink(source, "observation", index)));
    if (sample.length < data.observations.length) observations.append(node("p", "compare-help", "这里展示前 5 条，其余可在正文解析内容中查看。"));
    slot.append(observations);
  }
}

function renderMailBody(content, job, state) {
  const section = node("section", "reading mail-body-card");
  section.id = "mail-body-card";
  section.tabIndex = -1;
  const heading = node("div", "mail-body-title");
  heading.append(node("h3", "", "邮件正文"));
  section.append(heading);
  content.append(section);
  const bodies = job.materials.filter((item) => item.kind === "body");
  if (bodies.length !== 1) {
    section.append(node("p", "compare-help", bodies.length ? reasons.ambiguous_body : reasons.no_current_body));
  } else {
    const [body] = bodies;
    const actions = node("div", "compare-actions");
    actions.append(badge(body.state));
    heading.append(actions);
    if (["ready", "partial"].includes(body.state)) {
      const preview = node("div", "mail-body-preview", "正在读取正文预览…");
      section.append(preview);
      const show = (data) => {
        if (!section.isConnected) return;
        preview.replaceChildren();
        if (data.mail_preview) renderMailPreviewNotes(preview, data.mail_preview);
        const viewport = node("div", "mail-body-viewport");
        viewport.id = "mail-body-viewport";
        const frame = data.mail_preview ? originalMailFrame(job, body) : null;
        if (frame) viewport.append(frame);
        else {
          const text = body.format === "html" ? data.lines.join("\n") : data.original;
          viewport.append(node("pre", "raw", text || "没有可显示的正文文字。"));
        }
        preview.append(viewport);
        const toggle = button("", () => {
          state.bodyExpanded = !state.bodyExpanded;
          update();
        }, "control");
        const update = () => {
          viewport.classList.toggle("expanded", state.bodyExpanded);
          if (frame) frame.inert = !state.bodyExpanded;
          toggle.textContent = state.bodyExpanded ? "收起正文" : "展开正文";
          toggle.setAttribute("aria-expanded", String(state.bodyExpanded));
        };
        toggle.id = "mail-body-toggle";
        toggle.setAttribute("aria-controls", viewport.id);
        update();
        actions.append(toggle);
      };
      const cached = detailCache.get(keyOf(job.id, body.id));
      if (cached) show(cached);
      else fetchDetail(job.id, body.id).then(show, () => {preview.textContent = "无法加载正文预览。";});
      const parsed = node("details", "mail-body-parsed"), stage = node("div", "mail-body-parsed-content");
      const back = node("div", "source-return");
      back.id = "mail-body-source-return";
      renderSourceReturn(back, job, state, state.bodySource?.origin);
      parsed.id = "mail-body-parsed";
      parsed.open = state.bodyParsedExpanded;
      stage.id = "mail-body-parsed-content";
      stage.tabIndex = -1;
      parsed.append(node("summary", "", "正文解析内容"), back, stage);
      section.append(parsed);
      parsed.addEventListener("toggle", () => {
        if (!parsed.isConnected) return;
        state.bodyParsedExpanded = parsed.open;
        if (parsed.open && !stage.childElementCount) renderBodyParsing(stage, job, body, state);
      });
      if (parsed.open) renderBodyParsing(stage, job, body, state);
    } else {
      section.append(node("p", "compare-help", reason(body.reason) || "正文正在解析。"));
    }
  }
}

function openBodySource(job, body, source, origin) {
  if (!isComparing(job.id)) return;
  const parsed = $("mail-body-parsed"), stage = $("mail-body-parsed-content");
  if (!parsed || !stage) return;
  const state = comparisonState(job.id);
  state.bodySource = {source, origin};
  state.bodyParsedExpanded = true;
  parsed.open = true;
  renderSourceReturn($("mail-body-source-return"), job, state, origin);
  renderBodyParsing(stage, job, body, state, true);
}

function renderBodyParsing(stage, job, body, state, navigate = false) {
  const evidence = state.bodySource;
  const current = () => stage.isConnected && state.bodySource === evidence;
  const show = (data) => {
    if (!current()) return;
    stage.replaceChildren();
    renderMaterial(stage, job, body, data, {mode: "parsed"});
    if (evidence) {
      showUnlistedTextSource(stage, data, evidence.source);
      markSource(stage, evidence.source, navigate && stage.parentElement.open);
    }
  };
  const cached = detailCache.get(keyOf(job.id, body.id));
  if (cached) show(cached);
  else {
    stage.replaceChildren(empty("正在加载正文解析内容。"));
    fetchDetail(job.id, body.id).then(show, () => {
      if (current()) stage.replaceChildren(alert("无法加载正文解析内容。"));
    });
  }
}

function renderComparison(content, job) {
  const state = comparisonState(job.id);
  const inquiry = node("section", "reading inquiry-overview");
  inquiry.id = "inquiry-overview";
  content.append(inquiry);
  renderInquiryOverview(inquiry, job, state);
  if (job.materials.length) renderMailBody(content, job, state);
  const review = node("div");
  review.id = "mail-attachment-review";
  review.tabIndex = -1;
  content.append(review);
  renderAttachmentComparison(review, job, state);
}

function renderAttachmentComparison(content, job, state, {group = false, groupItems = []} = {}) {
  const panel = node("section", "reading compare-panel");
  const options = job.materials.filter(isAttachment);
  panel.append(node("h3", "", `邮件附件（${options.length}）`),
    node("p", "compare-help", group ? "点击附件，在下方看原件、解析内容和字段来源。" :
      "点文件名在下方预览，勾选要核对的附件。合计仅覆盖所选附件。"));
  content.append(panel);
  if (job.reason) panel.append(alert(reason(job.reason)));
  if (!options.length) {
    panel.append(empty(["queued", "running"].includes(job.state) ? "正在识别邮件材料。" : "这封邮件没有可选的附件。"));
    return;
  }
  const choices = node("fieldset", "compare-choices");
  choices.append(node("legend", "", group ? "本邮件附件" : "选择附件"));
  for (const item of options) {
    const row = node("div", "compare-choice-row");
    const check = node("input");
    check.type = "checkbox"; check.id = `choose-${job.id}-${item.id}`;
    check.setAttribute("aria-label", `选入核对：${materialName(item)}`);
    check.checked = state.ids.has(item.id);
    check.disabled = !item.comparison_eligible || item.state === "skipped";
    check.addEventListener("change", () => {
      if (check.checked) state.ids.add(item.id); else state.ids.delete(item.id);
      state.revision += 1; state.result = null; state.loading = false; state.error = "";
      refreshComparison(job);
      $(check.id)?.focus({preventScroll:true});
    });
    const preview = button(materialName(item), () => openAttachment(job, item.id, {toggle: true}), "compare-link attachment-name");
    preview.dataset.previewId = item.id;
    preview.setAttribute("aria-controls", "attachment-preview");
    preview.setAttribute("aria-expanded", String(state.preview?.id === item.id));
    if (!group) row.append(check);
    row.append(preview, badge(item.state));
    if (group) {
      const classified = groupItems.find((entry) => entry.id === item.id);
      if (classified?.duplicate_of) row.append(groupBadge("duplicate", "重复附件"));
      if (item.reason && ["unsupported", "failed"].includes(item.state)) {
        row.append(node("span", "compare-help", reason(item.reason)));
      }
    }
    choices.append(row);
  }
  panel.append(choices);
  const controls = node("div", "compare-actions");
  const pending = job.materials.some((item) => (item.kind === "body" || state.ids.has(item.id))
    && ["queued", "running"].includes(item.state));
  const compare = button(state.loading ? "正在核对…" : "核对所选附件", () => compareAttachments(job, state), "primary");
  compare.disabled = !state.ids.size || state.loading || pending;
  controls.append(compare, node("span", "", `已选 ${state.ids.size} 个附件${pending ? "，等待材料解析" : ""}`));
  if (!group) panel.append(controls);
  if (!group && state.error) panel.append(alert(state.error));
  const preview = node("section", "reading attachment-preview");
  preview.id = "attachment-preview";
  content.append(preview);
  renderAttachmentPreview(preview, job, state);
  if (group || !state.result) return;
  const result = state.result, outcome = node("section", "compare-result");
  outcome.id = "comparison-result"; outcome.tabIndex = -1;
  if (result.duplicates.length) outcome.append(alert("选中的附件包含内容相同的文件，已暂停求和。请核对后取消勾选重复附件。"));
  outcome.append(node("h3", "", "正文与所选附件对照"), comparisonTable("正文与附件合计", ["字段", "正文", "所选附件合计", "对照"],
    compareNames.map((name) => {
      const total = result.totals[name];
      return [names[name], compareField(job, result.body, name, state),
        compareValue({value: total.selected_document_sum, unit: total.unit, status: total.selected_status}, name),
        badge(total.comparison)];
    })));
  outcome.append(node("h3", "", "所选附件明细"), comparisonTable("所选附件逐件字段", ["附件", ...compareNames.map((n) => names[n])],
    result.attachments.map((item) => [button(materialName(item), () => openAttachment(job, item.id), "compare-link"),
      ...compareNames.map((name) => compareField(job, item, name, state))])));
  content.append(outcome);
}

function openAttachment(job, id, {toggle = false, source = null, origin = null} = {}) {
  if (!isComparing(job.id)) return;
  const host = $("attachment-preview");
  if (!host) return;
  const state = comparisonState(job.id);
  const same = state.preview?.id === id;
  if (same && toggle) state.preview = null;
  else if (!same || source) state.preview = {id, mode: source ? "parsed" : "original", position: newPosition(), source, origin};
  const triggers = $("mail-attachment-review").querySelectorAll("[data-preview-id]");
  for (const trigger of triggers) {
    trigger.setAttribute("aria-expanded", String(state.preview?.id === trigger.dataset.previewId));
    if (!state.preview && trigger.dataset.previewId === id) trigger.focus();
  }
  renderAttachmentPreview(host, jobs.get(job.id), state, true);
}

function renderAttachmentPreview(host, job, state, reveal = false) {
  const preview = state.preview;
  const item = job.materials.find((row) => row.id === preview?.id && isAttachment(row));
  host.replaceChildren();
  host.hidden = !item;
  if (!item) return;
  const head = node("div", "mail-body-title");
  const title = node("h3", "", `附件预览：${materialName(item)}`);
  title.id = "attachment-preview-title";
  title.tabIndex = -1;
  host.setAttribute("aria-labelledby", title.id);
  head.append(title, button("收起附件预览", () => openAttachment(job, item.id, {toggle: true}), "control"));
  const tabs = node("div", "tabs attachment-tabs"), stage = node("div", "attachment-content");
  const back = node("div", "source-return");
  renderSourceReturn(back, job, state, preview.origin);
  stage.tabIndex = -1;
  host.append(head, tabs, back, stage);
  let data = detailCache.get(keyOf(job.id, item.id)), failed = false;
  const showSource = (source) => {
    preview.source = source;
    preview.mode = "parsed";
    draw(true);
  };
  const tabControls = new Map();
  const modes = [["original", "原件预览"], ["parsed", "解析内容"]];
  if (job.route && job.route !== "preview" && supportsPipeline(item)) modes.push(["chain", "解析链路"]);
  for (const [mode, label] of modes) {
    const control = button(label, () => {preview.mode = mode; preview.source = null; draw();});
    control.id = `attachment-tab-${mode}`;
    tabControls.set(mode, control);
    tabs.append(control);
  }
  function draw(navigateSource = false) {
    if (!stage.isConnected) return;
    for (const [mode, control] of tabControls) control.setAttribute("aria-pressed", String(mode === preview.mode));
    stage.replaceChildren();
    if (preview.source && data) positionAtSource(preview.position, data, preview.source);
    renderMaterial(stage, job, item, data, {mode: preview.mode, position: preview.position, onSource: showSource,
      redraw: () => {preview.source = null; draw();}});
    if (!data && hasDetail(item)) {
      if (failed) stage.append(alert("无法加载此附件的解析结果。"));
      else if (preview.mode === "parsed" || !stage.childElementCount) stage.append(empty("正在加载附件内容。"));
    }
    if (preview.mode === "parsed" && data?.fields.length) {
      const fields = node("details", "attachment-fields"), entries = node("div");
      fields.append(node("summary", "", "业务字段与来源"), entries);
      renderFieldEntries(entries, data, "all", showSource);
      stage.append(fields);
    }
    if (preview.source && data) markSource(stage, preview.source, navigateSource);
  }
  draw(reveal);
  if (reveal && !(preview.source && data)) {
    title.scrollIntoView({block: "nearest"});
    title.focus({preventScroll: true});
  }
  if (!data && hasDetail(item)) fetchDetail(job.id, item.id).then((result) => {
    data = result;
    draw(reveal);
  }, () => {failed = true; draw();});
}

function disclosure(label, text) {
  const block = node("details");
  block.append(node("summary", "", label), node("pre", "quoted", text));
  return block;
}

function renderLines(lines) {
  if (!lines?.length) return empty("当前范围没有读到文字。");
  const reading = node("div", "reading");
  lines.forEach((line, index) => {
    const text = typeof line === "string" ? line : line.text;
    if (!text?.trim()) return;
    const row = node("div", "line");
    row.dataset.loc = typeof line === "object" && line.pointer ? line.pointer : String(index + 1);
    row.append(node("span", "lineno", index + 1), node("span", "", text));
    reading.append(row);
  });
  return reading.childElementCount ? reading : empty("当前范围没有读到文字。");
}

function renderImageEvidence(content, data, url, filename) {
  const ns = "http://www.w3.org/2000/svg";
  const [width, height] = data.size;
  const graphic = document.createElementNS(ns, "svg");
  graphic.classList.add("ocr-evidence");
  graphic.setAttribute("viewBox", `0 0 ${width} ${height}`);
  graphic.setAttribute("role", "img");
  graphic.setAttribute("aria-label", `${filename} · OCR 来源位置`);
  const image = document.createElementNS(ns, "image");
  image.setAttribute("href", url);
  image.setAttribute("width", width);
  image.setAttribute("height", height);
  graphic.append(image);
  data.lines.forEach((line, i) => {
    if (!line.box?.length) return;
    const area = document.createElementNS(ns, "polygon");
    area.dataset.loc = String(i + 1);
    area.setAttribute("points", line.box.map((point) => point.join(",")).join(" "));
    graphic.append(area);
  });
  content.append(graphic);
}

const pageOCR = new Map();
async function supplementPDFPage(url, page, control, status) {
  const path = `${url}/ocr/${page}`, round = roundVersion;
  if (pageOCR.get(path) === "running") return;
  pageOCR.set(path, "running");
  control.disabled = true;
  status.textContent = "正在本机补读当前页…";
  try {
    let result = await request(path, {method:"POST", headers:{"X-DocCase-OCR":"page"}});
    while (result.state === "running" && round === roundVersion) {
      await new Promise((resolve) => setTimeout(resolve, 800));
      result = await request(path);
    }
    if (round !== roundVersion) return;
    if (result.state !== "complete") throw new Error(result.reason);
    const [,jobId,itemId] = url.match(/\/jobs\/([^/]+)\/materials\/([^/]+)/);
    const [updated, job] = await Promise.all([request(url), request(`/api/jobs/${jobId}`)]);
    if (round !== roundVersion) return;
    pageOCR.set(path, "complete");
    detailCache.set(keyOf(jobId,itemId), updated);
    jobs.set(jobId, job);
    if (view === "group") await syncGroup(false, true);
    else if (activeJob === jobId) {
      if (activeItem === itemId) detail = updated;
      renderQueue(); renderReader(); renderFields();
    }
  } catch (error) {
    if (round === roundVersion) {
      pageOCR.set(path, "failed");
      control.disabled = false;
      status.textContent = ({
        ocr_agent_running:"该业务组正在用 DeepSeek，结束后可补读。",
        ocr_busy:"此 PDF 有一页正在补读。",
        ocr_read_unavailable:"这份 PDF 没有可复用的本地读取结果。",
      })[error.message] || "补读未完成，已保留原有结果。";
    }
  } finally {
    if (pageOCR.get(path) === "running") pageOCR.delete(path);
  }
}

function renderPDF(content, row, url, data, {mode, position, redraw, onSource}) {
  const {page} = position;
  const bar = node("div", "viewbar"), controls = node("div");
  const go = (target) => {position.page = target; redraw(); content.querySelector("select")?.focus();};
  const prev = button("上一页", () => go(page - 1), "control"); prev.disabled = page <= 1;
  const next = button("下一页", () => go(page + 1), "control"); next.disabled = page >= row.pages;
  const selectPage = node("select"); selectPage.setAttribute("aria-label", "PDF 页码");
  for (let i = 1; i <= row.pages; i++) selectPage.append(new Option(`第 ${i} / ${row.pages} 页`, i, false, i === page));
  selectPage.addEventListener("change", () => go(+selectPage.value));
  controls.append(prev, selectPage, next);
  bar.append(controls, node("span", "", mode === "original" ? "PDF 原始页面" : "当前页读取内容"));
  if (data?.chain && ["docling","deepseek-group"].includes(data.chain.route)
      && ["success","partial_success"].includes(data.chain.read.state)) {
    const pass = data.pdf_passes?.find((p) => p.kind === "ocr" && p.status === "success" && p.pages?.[0] === page);
    const running = pageOCR.get(`${url}/ocr/${page}`) === "running";
    const status = node("span", "compare-help", pass ? `第 ${page} 页已用本地 OCR 读取` : running ? "正在本机补读当前页…" : "");
    status.setAttribute("role", "status");
    if (!pass) {
      const action = button("补 OCR 当前页", () => supplementPDFPage(url, page, action, status), "control");
      action.disabled = running;
      bar.append(action);
    }
    bar.append(status);
  }
  content.append(bar);
  if (mode === "original") {
    const image = node("img", "docpage");
    image.alt = `${row.filename} 第 ${page} 页`;
    image.src = `${url}/page/${page}`;
    image.onerror = () => image.replaceWith(empty("无法生成此页预览。"));
    content.append(image);
    return;
  }
  if (!data) return content.append(empty("解析完成后，文字和识别出的表格将在这里显示。"));
  const selected = data.pages.find((value) => value.number === page);
  if (!selected) return;
  if (selected.clipped) content.append(alert("此页仅展示部分解析内容。"));
  const packing = (data.packing_rows || []).filter((item) => item.page === page);
  if (packing.length) {
    const columns = ["product_name", "ctns", "line_net_weight", "line_gross_weight", "line_volume",
      "unit_net_weight", "unit_gross_weight", "length_cm", "width_cm", "height_cm"];
    const labels = {line_net_weight:"行净重", line_gross_weight:"行毛重", line_volume:"行体积"};
    content.append(node("h3", "", "装箱货行"), node("p", "compare-help",
      "点数值定位原页中的文字或单元格。未能确认的值保留未确定，货行不计入整票合计。"),
    tableOf(["识别序号", ...columns.map((name) => labels[name] || names[name])], packing.map((row) => [
      row.ordinal, ...columns.map((name) => {
        const field = row.fields[name];
        if (!field) return "未确定";
        const value = resultText(field.value, field.unit);
        const source = field.sources[0];
        return source && onSource ? {content:button(value, () => onSource(source), "compare-link")} : value;
      }),
    ])));
  }
  for (const [index, table] of selected.tables.entries()) {
    content.append(node("h3", "", `识别表格 ${index + 1}`), pdfTable(table));
  }
  content.append(node("h3", "", "读取文字"), renderLines(selected.lines));
  const rows = data.cargo_rows.filter((item) => item.source?.page === page);
  if (rows.length) {
    content.append(node("h3", "", "货物明细"), node("p", "compare-help", "下表按原件货行展示，不从整票总计或邻行补值。"),
      tableOf(["原文序号", "品名", "行净重", "行毛重", "行体积", "来源"], rows.map((item) => [
      item.item_no, item.product_name, ...["line_net_weight", "line_gross_weight", "line_volume"].map((name) => {
        const field = item[name];
        return {text: field?.value == null ? "未确定" : resultText(field.value, field.unit),
          locator: field?.source?.value_pointer};
      }),
      item.source.description_pointer && onSource ? {content:sourceButton(
        {page, pointer:item.source.description_pointer, excerpt:item.product_name}, () => onSource(
          {page, pointer:item.source.description_pointer, excerpt:item.product_name}
        ))} : `第 ${page} 页`,
    ])));
  }
  const observations = [...data.observations, ...data.dimensions].filter((item) => (item.source?.page || item.page) === page);
  if (observations.length) {
    content.append(node("h3", "", "原文观察"), node("div", "reading", observations.map((item) =>
      item.raw_text || item.text || item.raw || `${names[item.field_name] || item.field_name || "尺寸"}：${resultText(item.value, item.unit)}`).join("\n")));
  }
}

function pdfTable(table) {
  const wrap = node("div", "grid-wrap"), grid = node("table"), body = node("tbody");
  const rows = new Map();
  for (const cell of table.cells) {
    if (!rows.has(cell.row)) rows.set(cell.row, []);
    rows.get(cell.row).push(cell);
  }
  const spans = new Map();
  for (const [row, cells] of Array.from(rows.entries()).sort((a,b) => a[0] - b[0])) {
    const tr = node("tr");
    let column = 0;
    for (const cell of cells.sort((a,b) => a.col - b.col)) {
      while (column < cell.col) {
        if ((spans.get(column) || 0) <= row) tr.append(node("td"));
        column += 1;
      }
      const td = node("td", "", cell.text);
      td.colSpan = Math.max(1, cell.col_end - cell.col);
      td.rowSpan = Math.max(1, cell.row_end - cell.row);
      td.dataset.loc = cell.pointer;
      tr.append(td);
      for (let c = cell.col; c < cell.col_end; c += 1) spans.set(c, cell.row_end);
      column = cell.col_end;
    }
    body.append(tr);
  }
  grid.append(body); wrap.append(grid);
  return wrap;
}

function tableOf(headers, rows) {
  const wrap = node("div", "grid-wrap"), grid = node("table"), thead = node("thead"), head = node("tr");
  headers.forEach((label) => {const th = node("th", "", label); th.scope = "col"; head.append(th);});
  thead.append(head);
  const body = node("tbody");
  for (const row of rows) {
    const tr = node("tr");
    row.forEach((value) => {
      if (value?.covered) return;
      const cell = value && typeof value === "object" ? value : null;
      const td = node("td", "", cell ? cell.text : value);
      if (cell?.content) td.append(cell.content);
      if (cell?.locator) td.dataset.loc = cell.locator;
      if (cell?.colSpan) td.colSpan = cell.colSpan;
      if (cell?.rowSpan) td.rowSpan = cell.rowSpan;
      tr.append(td);
    });
    body.append(tr);
  }
  grid.append(thead, body); wrap.append(grid);
  return wrap;
}

function sheetGrid(sheet, rows, merged) {
  const layout = new Map();
  let end = Math.max(...sheet.rows.map((r) => r.number));
  if (merged) for (const span of sheet.merges || []) {
    const keys = [];
    for (let r = span.row; r <= span.row_end; r++) for (let c = span.col; c <= span.col_end; c++) keys.push(`${r}/${c}`);
    if (keys.some((key) => layout.has(key))) continue;
    layout.set(keys[0], {colSpan:span.col_end - span.col + 1, rowSpan:span.row_end - span.row + 1});
    for (const key of keys.slice(1)) layout.set(key, {covered:true});
    end = Math.max(end, span.row_end);
  }
  return tableOf(["行", ...sheet.columns], Array.from({length:end}, (_, index) => {
    const n = index + 1, cells = rows.get(n);
    return [n, ...sheet.columns.map((col, c) => ({text:cells?.[col]?.value ?? "", locator:`${col}${n}`,
      ...layout.get(`${n}/${c + 1}`)}))];
  }));
}

function renderPacking(content, sheet, rows) {
  const packing = sheet.packing;
  const labels = {product_name:"品名", style:"款号", size:"尺码", color:"颜色", ctns:"纸箱箱数", item_quantity:"商品件数",
    line_gross_weight:"总毛重", line_net_weight:"总净重", line_volume:"总体积", carton_size:"箱规"};
  const columns = Object.entries(packing.columns);
  const lines = [...packing.detail_rows, packing.total_row].map((n) => [n === packing.total_row ? `${n}（合计）` : n,
    ...columns.map(([, col]) => ({text: rows.get(n)?.[col]?.value ?? "", locator:`${col}${n}`}))]);
  content.append(node("h3", "", "装箱明细"), node("p", "sheet-note",
    `${packing.detail_rows.length} 条明细 · 表头第 ${packing.header_row} 行 · 合计第 ${packing.total_row} 行 · 箱数和商品件数分列`),
    tableOf(["原表行", ...columns.map(([name]) => labels[name])], lines));
}

function renderSheet(content, data, {mode, position, redraw}) {
  if (!data.sheets.length) return content.append(empty("工作簿没有可见的工作表。"));
  const sheet = data.sheets[position.sheetIndex];
  const bar = node("div", "viewbar"), control = node("select");
  control.setAttribute("aria-label", "工作表");
  data.sheets.forEach((item, index) => control.append(new Option(item.name, index, false, index === position.sheetIndex)));
  control.addEventListener("change", () => {position.sheetIndex = +control.value; redraw(); content.querySelector("select")?.focus();});
  bar.append(control, node("span", "", mode === "original" ? "工作表原始单元格" : "读取到的单元格"));
  content.append(bar);
  if (sheet.rows.length) {
    const rows = new Map(sheet.rows.map((row) => [row.number, row.cells]));
    if (mode === "parsed" && sheet.packing) {
      renderPacking(content, sheet, rows);
      content.append(node("h3", "", "全部单元格"));
    }
    content.append(sheetGrid(sheet, rows, mode === "original"));
  }
  else content.append(empty("读取范围内没有单元格值。"));
  content.append(node("p", "sheet-note", "仅显示可见工作表。公式只读取已有缓存；每张表读取前 300 行和 50 列。"));
}

function sourceTitle(source) {
  if (source.sheet) return `${source.sheet}!${source.cell}${source.end_cell ? `:${source.end_cell}` : ""}`;
  if (source.page) return `第 ${source.page} 页`;
  if (source.table) return `表格 ${source.table} · 行 ${source.row} 列 ${source.column}`;
  if (source.box && source.line) return `原图区域 · 第 ${source.line} 行`;
  if (source.line) return `第 ${source.line} 行`;
  if (source.pointer) return "Docling 证据";
  return "来源";
}

function follow(source) {
  view = "parsed";
  positionAtSource(materialPosition, detail, source);
  renderReader();
  const panel = $("content");
  showUnlistedTextSource(panel, detail, source);
  markSource(panel, source, true);
}

function showUnlistedTextSource(panel, data, source) {
  if (source.pointer && source.excerpt && data?.type === "text"
      && !Array.from(panel.querySelectorAll("[data-loc]")).some((el) => el.dataset.loc === source.pointer)) {
    const evidence = node("section", "reading");
    evidence.append(node("h3", "", "字段来源片段"), renderLines([{text:source.excerpt, pointer:source.pointer}]));
    panel.prepend(evidence);
  }
}

function positionAtSource(position, data, source) {
  if (source.page) position.page = source.page;
  if (source.sheet && data.sheets) position.sheetIndex = Math.max(0, data.sheets.findIndex((sheet) => sheet.name === source.sheet));
}

function markSource(content, source, navigate) {
  const highlight = [source.cell || source.pointer || String(source.line || ""), source.end_cell].filter(Boolean);
  const targets = Array.from(content.querySelectorAll("[data-loc]")).filter((el) => highlight.includes(el.dataset.loc));
  for (const target of targets) {
    target.classList.add("source-hit");
    for (let ancestor = target.parentElement; ancestor && ancestor !== content; ancestor = ancestor.parentElement) {
      if (ancestor.tagName === "DETAILS") ancestor.open = true;
    }
  }
  if (!targets.length) content.prepend(alert("已打开来源所在页面；此片段没有独立显示。"));
  if (navigate) {
    (targets[0] || content.firstElementChild)?.scrollIntoView({block: "nearest", inline: "nearest"});
    content.focus({preventScroll: true});
  }
}

function sourceButton(source, action = () => follow(source)) {
  const el = button("", action, "source");
  el.append(node("b", "", sourceTitle(source)), node("span", "", source.excerpt));
  return el;
}

function renderFields() {
  const filters = $("field-filters"), container = $("fields");
  filters.replaceChildren(); container.replaceChildren();
  $("field-count").textContent = "";
  $("inspector-title").textContent = "业务字段";
  if (!detail?.fields.length) return container.append(empty("选择已解析的材料后，在这里核对对应字段。"));
  const all = detail.fields;
  const known = all.filter((field) => field.status === "known").length;
  const job = jobs.get(activeJob), row = job?.materials.find((item) => item.id === activeItem);
  const body = row?.kind === "body";
  $("field-count").textContent = `${body ? "正文 · " : ""}${known} / ${all.length} 有值`;
  if (body && !known) {
    const attachments = job.materials.filter(isAttachment);
    const target = attachments.find((item) => item.known > 0);
    if (attachments.length) {
      const note = node("div", "field-note");
      note.append(node("p", "", target ?
        "正文暂未确认字段，附件已有单独的解析结果。" : "这里统计正文；附件字段单独显示。"));
      note.append(button(target ? "查看已有值的附件" : "返回整封邮件看附件",
        () => target ? select(job.id, target.id) : select(job.id, null, true), "control"));
      container.append(note);
    }
  }
  for (const [id, label] of [["all", "全部"], ["known", "有来源"], ["review", "待核对"]]) {
    const el = button(label, () => {filter = id; renderFields(); $("field-filters").querySelector('[aria-pressed="true"]')?.focus();});
    el.setAttribute("aria-pressed", String(filter === id)); filters.append(el);
  }
  renderFieldEntries(container, detail, filter, follow);
}

function renderFieldEntries(container, data, filter, onSource) {
  const all = data.fields;
  const shown = all.filter((field) => filter === "all" || (filter === "known") === (field.status === "known"));
  if (!all.some((field) => field.status === "known") && data.type === "text" && filter !== "known") {
    container.append(node("p", "field-note", "当前材料已读取，暂未确认可提取的业务字段。可在「解析内容」核对文字。"));
  }
  if (data.type === "text" && data.observations?.length && filter !== "known") {
    const observations = node("section", "field-observations");
    observations.append(node("h3", "", "原文待归类"), node("p", "field-reason", "原文未说明包装口径，或未区分毛重和净重。"));
    const sources = node("div", "sources");
    data.observations.slice(0, 5).forEach((source) => sources.append(sourceButton(source, (event) => onSource(source, event.currentTarget))));
    observations.append(sources);
    container.append(observations);
  }
  if (!shown.length) return container.append(empty("没有符合此筛选条件的字段。"));
  for (const field of shown) {
    const entry = node("article", "field"), head = node("div", "field-top");
    const label = node("span", "field-name", names[field.name] || field.name);
    label.title = field.name;
    head.append(label, badge(field.status));
    entry.append(head, node("p", "field-value", resultText(field.value, field.unit || (field.name === "package_count" ? "package" : null))));
    if (field.status !== "known") entry.append(node("p", "field-reason", reason(field.reason) || "当前证据不足。"));
    if (field.sources.length) {
      const sources = node("div", "sources");
      for (const source of field.sources.slice(0, 5)) sources.append(sourceButton(source, (event) => onSource(source, event.currentTarget)));
      if (field.sources.length > 5) {
        const extra = node("details");
        extra.append(node("summary", "field-reason", `查看其余 ${field.sources.length - 5} 条来源`));
        const remaining = node("div", "sources");
        for (const source of field.sources.slice(5)) remaining.append(sourceButton(source, (event) => onSource(source, event.currentTarget)));
        extra.append(remaining);
        sources.append(extra);
      }
      entry.append(sources);
    }
    container.append(entry);
  }
}

function startNewRound() {
  roundVersion += 1; selectionVersion += 1;
  clearTimeout(poll); poll = null; refresh.previous = null;
  jobs.clear(); detailCache.clear(); detailRequests.clear(); comparisons.clear(); seen.clear();
  clearGroup();
  activeJob = null; activeItem = null; detail = null;
  renderQueue();
}

$("pick").addEventListener("click", () => {replaceOnPick = false; $("files").click();});
$("new-pick").addEventListener("click", () => {replaceOnPick = true; $("files").click();});
$("parse-route").addEventListener("change", updateRouteNote);
$("evaluation").addEventListener("toggle", () => {
  if ($("evaluation").open) loadEvaluation();
});
$("group-open").addEventListener("click", openGroup);
$("group-folder-pick").addEventListener("click", () => $("group-folder").click());
$("group-folder").addEventListener("change", (event) => {
  const emails = Array.from(event.target.files).filter((file) => /\.eml$/i.test(file.name));
  event.target.value = "";
  if (emails.length) enqueue(emails);
  else {
    $("notice").textContent = "所选目录内没有 EML 文件。";
    $("notice").hidden = false;
  }
});
checkConnection();
loadDemoCases();
$("files").addEventListener("change", (event) => {
  const files = Array.from(event.target.files);
  if (files.length && replaceOnPick) startNewRound();
  replaceOnPick = false; event.target.value = "";
  if (files.length) enqueue(files);
});
$("dropzone").addEventListener("dragover", (event) => {
  if (!event.dataTransfer.types.includes("Files")) return;
  event.preventDefault(); $("dropzone").classList.add("dragging");
});
$("dropzone").addEventListener("dragleave", (event) => {if (!$("dropzone").contains(event.relatedTarget)) $("dropzone").classList.remove("dragging");});
$("dropzone").addEventListener("drop", (event) => {
  event.preventDefault(); $("dropzone").classList.remove("dragging"); enqueue(Array.from(event.dataTransfer.files));
});
window.addEventListener("dragover", (event) => {if (event.dataTransfer.types.includes("Files")) event.preventDefault();});
window.addEventListener("drop", (event) => {if (event.dataTransfer.types.includes("Files")) event.preventDefault();});

$("sample").addEventListener("click", openDemoLibrary);
