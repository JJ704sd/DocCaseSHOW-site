"use strict";

const groupPaths = new Map();
const groupCatalog = new Map();
const groupReview = {ids: new Set(), current: null, first: null, reading: null, catalogKey: null, key: "", paintKey: "", result: null,
  pendingTarget:null, busy: false, starting: false, timer: null, error: "", open: new Set(), returnTo: null, scroll: 0};
const groupStates = {current: "当前邮件", duplicate: "重复", supplement: "补充", conflict: "冲突", uncertain: "未确定"};
const groupLimit = 12;

function clearGroup() {
  clearTimeout(groupReview.timer);
  groupPaths.clear();
  groupCatalog.clear();
  groupReview.ids.clear();
  Object.assign(groupReview, {current:null, first:null, reading:null, catalogKey:null, key:"", paintKey:"", result:null, pendingTarget:null,
    busy:false, starting:false, timer:null, error:"", open:new Set(), returnTo:null, scroll:0});
}

function registerGroupFile(id, path) {
  groupPaths.set(id, path);
  const parts = path.split(/[\\/]/);
  const folder = parts.slice(0, -1).findLastIndex((name) => /^G\d{3,}$/i.test(name));
  if (folder < 0) return;
  const key = "folder:" + parts.slice(0, folder + 1).join("/");
  const group = groupCatalog.get(key) || {kind:"folder", label:parts[folder], path:parts.slice(0, folder + 1).join(" / "), ids:new Set(), current:null};
  group.ids.add(id);
  groupCatalog.set(key, group);
}

function groupOrder(mail) {
  const t = Date.parse(mail.sent_at || mail.metadata?.[0]?.sent_at);
  return Number.isFinite(t) ? t : Infinity;
}

function byMailTime(a,b) {
  return groupOrder(a) - groupOrder(b) || a.filename.localeCompare(b.filename, "zh-CN");
}

function groupContaining(id) {
  return [...groupCatalog].find(([, group]) => group.ids.has(id))?.[0] || null;
}

function threadTokens(value) {
  return (value?.match(/<[^<>\s]+>/g) || []).slice(0, 128);
}

function rebuildThreadGroups() {
  const previous = [];
  const assigned = new Set();
  for (const [key, group] of groupCatalog) {
    if (group.kind === "thread") {
      previous.push(group);
      groupCatalog.delete(key);
    } else for (const id of group.ids) assigned.add(id);
  }
  const mails = selectedGroupJobs().filter((mail) => !assigned.has(mail.id)).sort(byMailTime);
  const owners = new Map(), adjacent = new Map(mails.map((mail) => [mail.id, new Set()]));
  const connect = (a,b) => {
    if (a === b) return;
    adjacent.get(a).add(b); adjacent.get(b).add(a);
  };
  for (const mail of mails) {
    const id = threadTokens(mail.metadata?.[0]?.thread_headers?.message_id)[0];
    if (!id) continue;
    if (owners.has(id)) connect(mail.id, owners.get(id));
    else owners.set(id, mail.id);
  }
  for (const mail of mails) {
    const headers = mail.metadata?.[0]?.thread_headers;
    for (const id of threadTokens([headers?.in_reply_to, ...(headers?.references || [])].join(" "))) {
      if (owners.has(id)) connect(mail.id, owners.get(id));
    }
  }
  const visited = new Set();
  for (const root of mails) {
    if (visited.has(root.id)) continue;
    const pending = [root.id], ids = new Set();
    visited.add(root.id);
    while (pending.length) {
      const id = pending.pop();
      ids.add(id);
      for (const reply of adjacent.get(id)) if (!visited.has(reply)) {
        visited.add(reply); pending.push(reply);
      }
    }
    const prior = previous.find((group) => ids.has(group.current));
    const current = prior?.current || root.id;
    const selection = prior?.selection && prior.ids.size === ids.size &&
      [...prior.ids].every((id) => ids.has(id)) ? prior.selection : null;
    groupCatalog.set("thread:" + root.id, {kind:"thread", ids, current, selection,
      label: root.metadata?.[0]?.subject || root.filename});
  }
}

function showImportedGroups(emailIds) {
  if (!activeJob) activeJob = emailIds[0];
  clearTimeout(groupReview.timer);
  selectionVersion += 1;
  view = "group";
  Object.assign(groupReview, {ids:new Set(), current:null, first:null, reading:null, catalogKey:null, result:null,
    key:"", paintKey:"", busy:false, error:"", open:new Set(), returnTo:null, scroll:0, pendingTarget:emailIds[0]});
  renderQueue();
  renderGroupReader();
}

function settleImportedGroups() {
  const target = groupReview.pendingTarget;
  if (!target || selectedGroupJobs().some((job) =>
    !job.metadata?.length && ["queued", "running"].includes(job.state))) return false;
  rebuildThreadGroups();
  activateGroup(groupContaining(target));
  return true;
}

function activateGroup(key) {
  const record = groupCatalog.get(key);
  const candidates = record?.selection || record?.ids;
  const ids = candidates && candidates.size <= groupLimit ? [...candidates].filter((id) => jobs.has(id)) : [];
  const ordered = ids.map((id) => jobs.get(id)).sort(byMailTime);
  const current = ids.includes(record?.current) ? record.current : ordered[0]?.id || null;
  clearTimeout(groupReview.timer);
  Object.assign(groupReview, {catalogKey:key, ids:new Set(ids), current, first:ids.includes(record?.first) ? record.first : null, reading:current, result:null,
    key:"", paintKey:"", pendingTarget:null, busy:false, error:"", open:new Set(), returnTo:null, scroll:0});
  renderQueue();
  renderGroupReader();
}

function renderGroupSidebar(list) {
  $("file-count").textContent = `${groupCatalog.size} 组`;
  for (const [key, record] of [...groupCatalog].sort(([, a], [, b]) => a.label.localeCompare(b.label, "zh-CN", {numeric:true}))) {
    const control = button("", () => activateGroup(key), "group-nav-item");
    control.dataset.row = key;
    control.setAttribute("aria-current", String(groupReview.catalogKey === key));
    control.append(node("strong", "", record.label), node("span", "", `${record.ids.size} 封邮件`));
    const mail = jobs.get(record.current || record.ids.values().next().value);
    const subject = mail?.metadata?.[0]?.subject;
    if (subject && subject !== record.label) control.append(node("span", "group-nav-subject", subject));
    if (record.path) control.append(node("small", "", record.path));
    if (record.kind === "thread") control.append(node("small", "", record.ids.size > 1 ? "按邮件回复关系归组" : "未关联其他已导入邮件"));
    list.append(control);
  }
  const add = button("手动建组", () => activateGroup(null), "group-nav-item group-new");
  add.dataset.row = "group-draft";
  add.setAttribute("aria-current", String(!groupReview.catalogKey));
  list.append(add);
}

function openGroup() {
  if ($("group-open").disabled) return;
  selectionVersion += 1;
  view = "group";
  if (!groupCatalog.size && activeJob && jobs.get(activeJob)?.is_email && !groupReview.pendingTarget) {
    showImportedGroups([activeJob]);
    return;
  }
  if (!groupReview.ids.size && !groupReview.pendingTarget && !groupReview.catalogKey && groupCatalog.size) {
    const key = Array.from(groupCatalog.entries()).find(([, record]) => record.ids.has(activeJob))?.[0]
      || groupCatalog.keys().next().value;
    activateGroup(key);
    $("group-title")?.focus({preventScroll:true});
    return;
  }
  renderQueue();
  renderGroupReader();
  if (!groupReview.returnTo) $("group-title")?.focus({preventScroll:true});
  else {
    const target = $(groupReview.returnTo);
    if (target) {
      for (let parent = target.parentElement; parent && parent !== $("content"); parent = parent.parentElement) {
        if (parent.tagName === "DETAILS") parent.open = true;
      }
      target.scrollIntoView({block:"center"});
      target.focus({preventScroll:true});
    }
    groupReview.returnTo = null;
  }
}

function renderGroupReader() {
  $("workspace").classList.remove("comparing");
  $("workspace").classList.add("group-workspace");
  $("mail-context").hidden = true;
  const title = node("h2", "", groupCatalog.get(groupReview.catalogKey)?.label || "业务组提取与核对");
  title.id = "group-title"; title.tabIndex = -1;
  const titleBlock = node("div");
  const record = groupCatalog.get(groupReview.catalogKey);
  titleBlock.append(title, node("p", "", record ? `业务组 · ${record.ids.size} 封邮件` : "按邮件阅读，按业务组核对字段"));
  $("material-head").replaceChildren(titleBlock);
  if (groupReview.reading || groupReview.current) $("material-head").append(
    button("查看单封邮件详情", () => select(groupReview.reading || groupReview.current, null, true), "control"));
  $("tabs").replaceChildren();
  $("tabs").hidden = true;
  $("content").replaceChildren();
  syncGroup(true);
  $("content").scrollTop = groupReview.scroll;
}

function selectedGroupJobs() {
  return Array.from(jobs.values()).filter((job) => job.is_email);
}

function groupSelectionChanged() {
  clearTimeout(groupReview.timer);
  if (!groupReview.ids.has(groupReview.first)) groupReview.first = null;
  if (!groupReview.ids.has(groupReview.reading)) groupReview.reading = groupReview.current;
  const record = groupCatalog.get(groupReview.catalogKey);
  if (record) {
    if (record.kind === "manual") record.ids = new Set(groupReview.ids);
    else record.selection = groupReview.ids.size === record.ids.size &&
      [...groupReview.ids].every((id) => record.ids.has(id)) ? null : new Set(groupReview.ids);
    record.current = groupReview.current;
    record.first = groupReview.first;
    renderQueue();
  }
  Object.assign(groupReview, {key:"", result:null, error:"", busy:false});
  syncGroup(true);
}

function groupQuery(current, selected) {
  return new URLSearchParams([["current", current], ...selected.map((mail) => ["email", mail.id]),
    ...(groupReview.first ? [["first_inquiry", groupReview.first]] : [])]);
}

async function syncGroup(force = false, refresh = false) {
  if (view !== "group") return;
  const emails = selectedGroupJobs();
  if (groupReview.pendingTarget) {
    if (!settleImportedGroups() && force) paintGroup(emails);
    return;
  }
  const stamp = (job) => [job.id, job.state, job.metadata?.[0]?.subject, job.materials.map((item) => [item.id, item.state, item.known])];
  const uiKey = JSON.stringify(emails.map(stamp));
  if (force || uiKey !== groupReview.paintKey) {
    groupReview.paintKey = uiKey;
    paintGroup(emails);
  }
  const selected = emails.filter((email) => groupReview.ids.has(email.id));
  if (!selected.length || !groupReview.current) return;
  const key = JSON.stringify([groupReview.current, groupReview.first, selected.map(stamp)]);
  if (key === groupReview.key && (!refresh || groupReview.busy)) {
    if (force && groupReview.result?.agent?.state === "running") pollGroupAgent();
    return;
  }
  groupReview.key = key;
  groupReview.busy = true;
  groupReview.error = "";
  paintGroup(emails);
  const query = groupQuery(groupReview.current, selected);
  try {
    const result = await request(`/api/group-review?${query}`);
    if (key === groupReview.key) groupReview.result = result;
  } catch (error) {
    if (key === groupReview.key) groupReview.error = reason(error.message) || "暂时无法读取业务组结果。";
  } finally {
    if (key === groupReview.key) {
      groupReview.busy = false;
      if (view === "group") {
        paintGroup(selectedGroupJobs());
        if (groupReview.result?.agent?.state === "running" && !groupReview.error) pollGroupAgent();
      }
    }
  }
}

function pollGroupAgent() {
  clearTimeout(groupReview.timer);
  groupReview.timer = setTimeout(() => syncGroup(false, true), 1000);
}

async function startGroupAgent() {
  if (groupReview.starting) return;
  const key = groupReview.key;
  const selected = selectedGroupJobs().filter((job) => groupReview.ids.has(job.id));
  const query = groupQuery(groupReview.current, selected);
  groupReview.starting = true;
  paintGroup(selectedGroupJobs());
  try {
    await request(`/api/group-extraction?${query}`, {method:"POST", headers:{"X-DocCase-Group":"deepseek"}});
    if (key === groupReview.key) await syncGroup(false, true);
  } catch (error) {
    if (key === groupReview.key) groupReview.error = ({
      group_agent_waiting:"请等所选邮件读取完毕。",
      group_agent_busy:"另一个业务组正在补提，请稍后再试。",
      group_agent_no_read:"所选邮件没有可供补提的正文或 PDF。",
    })[error.message] || "未能启动业务组补提。";
  } finally {
    groupReview.starting = false;
    if (view === "group") paintGroup(selectedGroupJobs());
  }
}

function groupBadge(value, label = groupStates[value]) {
  return node("span", `state group-state-${value}`, label);
}

function groupPath(mail, item) {
  const path = groupPaths.get(mail.id);
  const origin = path ? `业务组 / ${path}` : `业务组 / ${mail.filename}`;
  return item ? `${origin} / ${item.kind === "body" ? "正文" : `附件 / ${item.filename}`}` : origin;
}

async function groupOpenMaterial(mail, item, source = null, returnTo = null) {
  if (view === "group") {
    if (groupReview.reading !== mail.id) {
      groupReview.reading = mail.id;
      renderGroupReading($("group-reading"), groupReview.result);
    }
    const job = jobs.get(mail.id);
    if (!job) return;
    const origin = returnTo ? {section:"group", id:returnTo} : null;
    if (item.kind === "body") {
      if (source) openBodySource(job, item, source, origin);
      else jumpTo("mail-body-card");
    } else {
      openAttachment(job, item.id, {source, origin});
      if (!source) jumpTo("attachment-preview");
    }
    return;
  }
  groupReview.scroll = $("content").scrollTop;
  rememberGroupDisclosures();
  groupReview.returnTo = returnTo;
  await select(mail.id, item.id);
  if (source && view !== "group" && activeJob === mail.id && activeItem === item.id && detail) follow(source);
}

function rememberGroupDisclosures() {
  if ($("workspace").classList.contains("group-workspace") && $("content").querySelector(".group-setup")) {
    groupReview.open = new Set(Array.from($("workspace").querySelectorAll("details[open][id]"), (el) => el.id));
  }
}

function focusGroupResult(id) {
  const target = $(id);
  if (!target) return;
  for (let parent = target.parentElement; parent && parent !== $("fields"); parent = parent.parentElement) {
    if (parent.tagName === "DETAILS") parent.open = true;
  }
  target.scrollIntoView({block:"center", inline:"nearest"});
  target.focus({preventScroll:true});
}

function groupSource(mail, item, source, id) {
  const link = sourceButton(source, () => groupOpenMaterial(mail, item, source, id));
  link.id = id;
  return link;
}

function paintGroup(emails) {
  const content = $("content"), fields = $("fields"), scroll = content.scrollTop, fieldsScroll = fields.scrollTop;
  const focused = $("workspace").contains(document.activeElement) ? document.activeElement.id : "";
  rememberGroupDisclosures();
  content.replaceChildren();
  fields.replaceChildren();
  $("field-filters").replaceChildren();
  $("inspector-title").textContent = "业务组字段";
  $("field-count").textContent = groupReview.result ? `${groupReview.result.emails.length} 封邮件` : "";
  if (groupReview.pendingTarget) {
    content.append(empty("正在读取邮件头并整理业务组…"));
    fields.append(empty("业务组就绪后，将显示全组字段。"));
    return;
  }
  const record = groupCatalog.get(groupReview.catalogKey);
  const setup = node("section", "reading group-setup");
  const config = node("details", "group-configuration");
  config.id = "group-configuration";
  config.open = !groupReview.current || !groupReview.ids.size;
  config.append(node("summary", "", "调整组内邮件与参考邮件"), node("p", "compare-help",
    "明确目录优先；散传邮件按已导入邮件的回复关系归组。你可以调整成员。只有主题相似的邮件不会自动合并。"));
  const choices = node("div", "group-choices");
  for (const mail of [...emails].sort((a,b) => Number(record?.ids.has(b.id) || false) -
    Number(record?.ids.has(a.id) || false) || byMailTime(a,b))) {
    const checked = groupReview.ids.has(mail.id), row = node("div", "group-choice");
    const check = node("input"), radio = node("input"), label = node("label", "group-mail-label");
    check.type = "checkbox"; check.checked = checked; check.id = `group-choose-${mail.id}`;
    check.disabled = !checked && groupReview.ids.size >= groupLimit;
    check.setAttribute("aria-label", `纳入业务组：${mail.filename}`);
    check.addEventListener("change", () => {
      if (check.checked) groupReview.ids.add(mail.id);
      else {
        groupReview.ids.delete(mail.id);
        if (groupReview.current === mail.id) groupReview.current = null;
      }
      if (!groupReview.current && groupReview.ids.size === 1) groupReview.current = groupReview.ids.values().next().value;
      groupSelectionChanged();
    });
    label.htmlFor = check.id;
    label.append(node("strong", "", mail.metadata?.[0]?.subject || mail.filename), node("span", "group-path", groupPaths.get(mail.id) || mail.filename));
    radio.type = "radio"; radio.name = "group-current"; radio.id = `group-current-${mail.id}`;
    radio.checked = groupReview.current === mail.id; radio.disabled = !checked;
    radio.setAttribute("aria-label", `设为当前邮件：${mail.filename}`);
    radio.addEventListener("change", () => {groupReview.current = mail.id; groupSelectionChanged();});
    const currentLabel = node("label", "group-current-choice");
    currentLabel.append(radio, node("span", "", "参考邮件"));
    row.append(check, label, badge(mail.state), currentLabel);
    choices.append(row);
  }
  config.append(choices);
  const firstLabel = node("label", "group-current-choice", "已核实首询：");
  const firstSelect = node("select");
  firstSelect.setAttribute("aria-label", "已核实首询");
  const none = node("option", "", "未确认"); none.value = ""; firstSelect.append(none);
  for (const mail of emails.filter((mail) => groupReview.ids.has(mail.id))) {
    const option = node("option", "", mail.metadata?.[0]?.subject || mail.filename);
    option.value = mail.id; firstSelect.append(option);
  }
  firstSelect.value = groupReview.first || "";
  firstSelect.addEventListener("change", () => {groupReview.first = firstSelect.value || null; groupSelectionChanged();});
  firstLabel.append(firstSelect); config.append(firstLabel);
  if (!groupReview.catalogKey && groupReview.current && groupReview.ids.size) {
    const actions = node("div", "compare-actions"), name = node("input");
    name.type = "text"; name.placeholder = "业务组名称（可选）"; name.maxLength = 80;
    name.setAttribute("aria-label", "业务组名称");
    const save = button("保存为本轮业务组", () => {
      const key = "manual:" + groupReview.current + ":" + (groupCatalog.size + 1);
      const mail = jobs.get(groupReview.current);
      groupCatalog.set(key, {kind:"manual", label:name.value.trim() || mail.metadata?.[0]?.subject || mail.filename,
        ids:new Set(groupReview.ids), current:groupReview.current});
      groupReview.catalogKey = key;
      rebuildThreadGroups();
      renderQueue();
      renderGroupReader();
    }, "control");
    actions.append(name, save);
    config.append(actions);
  }
  setup.append(config);
  if (record?.demo) setup.append(renderDemoHint(record.demo));
  const progress = !groupReview.ids.size ? (record?.ids.size > groupLimit ?
    `该组有 ${record.ids.size} 封邮件。请选择最多 ${groupLimit} 封进行本轮核对。` : "请选择组内邮件。")
    : !groupReview.current ? "请从已选邮件中指定当前邮件。"
    : groupReview.busy && !groupReview.result ? "正在提取组内字段…" :
      groupReview.ids.size === 1 ? "当前组有 1 封邮件，可在上方加入相关邮件。" : `本次核对 ${groupReview.ids.size} 封邮件。`;
  setup.append(node("p", "group-progress", progress));
  if (groupReview.error) setup.append(alert(groupReview.error));
  content.append(setup);
  if (groupReview.result) {
    const reading = node("div");
    reading.id = "group-reading";
    content.append(reading);
    renderGroupReading(reading, groupReview.result);
    renderGroupResult(fields, groupReview.result);
  } else {
    fields.append(empty("选定一组邮件和参考邮件后，这里显示组内字段。"));
    if (!record && !groupReview.pendingTarget) content.append(
      button("打开内置经典案例", openDemoLibrary, "control"));
  }
  for (const id of groupReview.open) if ($(id)) $(id).open = true;
  content.scrollTop = scroll;
  fields.scrollTop = fieldsScroll;
  if (focused) $(focused)?.focus({preventScroll:true});
}

function renderGroupReading(host, result) {
  if (!host || !result) return;
  host.replaceChildren();
  const emails = [...result.emails].sort(byMailTime);
  const selected = emails.find((mail) => mail.id === groupReview.reading) || emails[0];
  groupReview.reading = selected.id;
  const timeline = node("section", "reading group-thread");
  timeline.append(node("h3", "", "组内邮件"), node("p", "compare-help", "切换阅读不会改变组内参考邮件。"));
  const cards = node("div", "group-mail-strip");
  for (const [index, mail] of emails.entries()) {
    const card = button("", () => {
      groupReview.reading = mail.id;
      const position = cards.scrollLeft;
      renderGroupReading(host, result);
      const strip = host.querySelector(".group-mail-strip");
      strip.scrollLeft = position;
      $(`group-read-${mail.id}`)?.focus({preventScroll:true});
    }, "group-thread-card");
    card.id = `group-read-${mail.id}`;
    card.setAttribute("aria-pressed", String(mail.id === selected.id));
    const labels = node("div", "compare-actions");
    if (index === 0 && Number.isFinite(groupOrder(mail))) labels.append(groupBadge("current", "组内最早"));
    if (mail.role === "current") labels.append(groupBadge("current", "参考邮件"));
    if (mail.duplicate_of) labels.append(groupBadge("duplicate", "重复邮件"));
    labels.append(badge(mail.state));
    card.append(labels, node("strong", "", mail.subject || mail.filename),
      node("small", "", mail.sent_at || "发送时间未读取"));
    cards.append(card);
  }
  timeline.append(cards);
  host.append(timeline);
  const job = jobs.get(selected.id);
  if (!job) return;
  const header = node("section", "reading group-reading-head");
  header.append(node("h3", "", selected.subject || selected.filename),
    node("p", "group-path", groupPath(selected)));
  const meta = job.metadata?.[0];
  if (meta?.from_addr) header.append(node("p", "compare-help", `发件人：${meta.from_addr}`));
  const to = meta?.to_addrs?.filter(Boolean).join("、");
  if (to) header.append(node("p", "compare-help", `收件人：${to}`));
  if (meta?.sent_at) header.append(node("p", "compare-help", `发送时间：${meta.sent_at}`));
  if (selected.role !== "current") header.append(button("设为组内参考邮件", () => {
    groupReview.current = selected.id; groupSelectionChanged();
  }, "control"));
  host.append(header);
  const state = comparisonState(job.id);
  renderMailBody(host, job, state);
  const attachments = node("div");
  attachments.id = "mail-attachment-review";
  host.append(attachments);
  renderAttachmentComparison(attachments, job, state, {group:true, groupItems:selected.attachments});
}

function renderGroupResult(content, result) {
  const body = node("section", "reading group-panel");
  const confirmed = result.group_fields.filter((f) => f.value != null).length;
  const conflicts = result.group_fields.filter((f) => f.status === "conflict" ||
    f.attachment_entries.some((entry) => entry.status === "conflict")).length;
  const pending = result.group_fields.filter((f) => f.status === "supplement").length;
  const contextCount = result.agent?.context?.supplements?.length || 0;
  body.append(node("h3", "", result.route_comparison ? "组内提取结果 · 规则 + DeepSeek" : "组内提取结果 · 本地规则"), node("p", "group-field-counts",
    `${confirmed} 项有组值 · ${pending} 项有候选 · ${conflicts} 项有冲突${contextCount ? ` · 组级补充 ${contextCount} 项` : ""}`),
    node("p", "compare-help", result.emails.some((mail) => ["queued","running"].includes(mail.state))
      ? "部分材料仍在解析，字段将自动更新。"
      : "组值来自参考邮件正文的明确证据。其他正文或附件作为候选；不同单据不相加。"));
  content.append(body);
  if (result.structured_result) renderLocalInquiryResult(content, result.structured_result, result.emails);
  if (result.body_measurements?.length) {
    const panel = node("section", "reading group-panel");
    panel.id = "group-body-measurements";
    panel.append(node("h3", "", "包装原文与体积估算"));
    for (const [index, measurement] of result.body_measurements.entries()) {
      const mail = result.emails.find((item) => item.id === measurement.email_id);
      if (mail?.body?.id !== measurement.material_id) continue;
      const row = node("div", "group-field-entry"), pack = measurement.package;
      row.append(node("p", "group-path", groupPath(mail, mail.body)),
        node("strong", "", `包装原文：${pack.count} ${pack.label}`),
        node("p", "compare-help", "通用包装表述；具体包装类型仍需确认。"));
      const estimate = measurement.volume_estimate;
      if (estimate) row.append(node("p", "", `单件体积估算：${resultText(estimate.value, estimate.unit)}`),
        node("p", "compare-help", `根据 ${estimate.dimensions.join(" × ")} ${estimate.dimension_unit} 计算；需确认是否代表整票，未计入组值。`));
      const sources = estimate?.sources || pack.sources;
      sources.forEach((source, i) => row.append(groupSource(mail, mail.body, source, `group-measurement-source-${index}-${i}`)));
      panel.append(row);
    }
    content.append(panel);
  }
  if (result.body_observations?.length) {
    const pendingSource = node("details", "reading group-panel");
    pendingSource.id = "group-body-observations";
    const total = result.body_observations.reduce((sum, item) => sum + item.total, 0);
    pendingSource.append(node("summary", "", `原文待归类（${total} 条）`), node("p", "compare-help",
      "这些行还不能区分商品件数与包装件数，或毛重、净重与计费重。可回到原文核对，不计入组值。"));
    const labels = {pieces:"件数口径未确认", weight:"重量类型未确认"};
    for (const item of result.body_observations) {
      const mail = result.emails.find((e) => e.id === item.email_id);
      if (mail?.body?.id !== item.material_id) continue;
      const row = node("div", "group-field-entry");
      row.append(node("p", "group-path", groupPath(mail, mail.body)));
      for (const [index, source] of item.sources.entries()) {
        row.append(node("p", "compare-help", labels[source.kind]),
          groupSource(mail, mail.body, source, `group-observation-${mail.id}-${index}`));
      }
      if (item.total > item.sources.length) row.append(node("p", "compare-help", "仅列出前 5 条，其余可查看正文。"));
      pendingSource.append(row);
    }
    content.append(pendingSource);
  }
  const agent = result.agent;
  if (agent?.enabled) {
    const panel = node("section", "reading group-panel");
    panel.append(node("h3", "", "DeepSeek 业务组补提"), node("p", "compare-help",
      "按材料提取所选邮件的正文、PDF 和可核验的单工作表 XLSX 字段。正文的地点、件数或重量口径不明时，可带上相邻行或同表片段。只向 DeepSeek 发送证据片段，不发送原件。"));
    if (agent.context_enabled) panel.append(node("p", "compare-help",
      "本服务已启用组级上下文补提。首轮仍缺字段时，可额外发送一次脱敏的同组证据；采纳值单独展示为补充。"));
    if (agent.state === "ready") {
      const run = button(groupReview.starting ? "正在启动…" : "用 DeepSeek 补提所选业务组", startGroupAgent, "primary");
      run.id = "group-run-agent"; run.disabled = groupReview.starting;
      panel.append(run);
    } else if (["complete", "partial"].includes(agent.state)) {
      panel.append(node("p", "group-progress",
        `首轮${agent.state === "partial" ? "部分完成" : "已完成"}：${agent.evaluated}/${agent.total} 份材料 · ${agent.requests} 次请求`),
        node("p", "group-field-counts", `采纳 ${agent.accepted} · 同值印证 ${agent.corroborated || 0} · 未通过 ${agent.rejected || 0}`));
      if (result.route_comparison) renderGroupRouteComparison(panel, result.route_comparison);
      if (agent.validation?.length) {
        const audit = node("details", "group-unknown"); audit.id = "group-agent-validation";
        audit.append(node("summary", "", "查看按材料的校验记录"));
        const outcomes = {accepted:"采纳", corroborated:"与已有值一致", rejected:"未通过", received:"收到候选",
          skipped:"未发送", fallback:"保留本地结果", failed:"未完成"};
        for (const item of agent.validation) {
          const mail = result.emails.find((e) => e.id === item.email_id);
          const material = mail?.body?.id === item.material_id ? mail.body :
            mail?.attachments.find((a) => a.id === item.material_id);
          if (!mail || !material) continue;
          const row = node("div", "group-field-entry");
          row.append(node("p", "group-path", groupPath(mail, material)));
          if (item.context_fragments) row.append(node("p", "compare-help",
            `成组上下文 ${item.context_fragments} 段 · ${item.context_fields.map((f) => names[f] || f).join("、")}`));
          for (const event of item.events) {
            const field = event.requirement_id?.replace(/^REQ-/, "").toLowerCase();
            const receipt = event.stage === "material_agent" && event.proposed != null
              ? `Markdown ${event.markdown_fragments || 0} / READ ${event.read_fragments || 0} 段 · 候选 ${event.proposed}` : "";
            const support = event.read_context_added ? `校验补查 ${event.read_context_added} 段原文` : "";
            row.append(node("p", "compare-help", [names[field], outcomes[event.outcome] || event.outcome,
              receipt, support, pipelineReason(event.reason)].filter(Boolean).join(" · ")));
          }
          audit.append(row);
        }
        panel.append(audit);
      }
      if (agent.context) renderGroupContextResult(panel, agent.context, result.emails, result.structured_result);
      if (agent.object_events?.length) renderObjectAudit(panel, agent.object_events, result.emails);
    } else panel.append(node("p", "group-progress", {waiting:"等待所选邮件读取完毕。", running:"正在补提所选业务组…",
      no_read:"所选邮件没有可供补提的正文或附件。"}[agent.state] || ""));
    content.append(panel);
  }
  const statuses = ["current", "duplicate", "conflict", "supplement", "uncertain"];
  const priority = ["cargo_origin", "pickup_city", "departure_port", "destination", "trade_terms",
    "package_count", "ctns", "shipment_gross_weight", "shipment_net_weight", "shipment_chargeable_weight",
    "line_gross_weight", "line_volume"];
  const rank = (field) => priority.includes(field.name) ? priority.indexOf(field.name) : priority.length;
  const emails = new Map(result.emails.map((mail) => [mail.id, mail]));
  const unknown = node("details", "group-unknown"); unknown.id = "group-body-unknown";
  const needsMaterialReview = (field) => field.attachment_entries.some((entry) => entry.status === "conflict");
  const hideUnknown = (field) => field.status === "uncertain" &&
    !priority.includes(field.name) && !needsMaterialReview(field);
  unknown.append(node("summary", "", `展开未确定字段（${result.group_fields.filter(hideUnknown).length}）`));
  for (const field of [...result.group_fields].sort((a,b) => statuses.indexOf(a.status) - statuses.indexOf(b.status) || rank(a) - rank(b))) {
    const row = node("details", "group-field"); row.id = `group-field-${field.name}`;
    const heading = node("summary"), values = node("span", "group-values");
    const bodyValues = field.candidates.filter((value) => value.scope === "body");
    const attachmentValues = field.candidates.filter((value) => value.scope === "attachment");
    const numberText = (entry) => resultText(entry.value, entry.unit || (field.name === "package_count" ? "package" : null));
    const preview = (entries) => entries.slice(0, 3).map(numberText).join(" / ") +
      (entries.length > 3 ? ` 等 ${entries.length} 个值` : "");
    const candidateLabel = bodyValues.length ? "正文候选" : "附件候选";
    values.textContent = field.value != null ? numberText(field)
      : bodyValues.length ? preview(bodyValues) : attachmentValues.length ? preview(attachmentValues)
      : field.status === "conflict" || needsMaterialReview(field) ? "展开核对不同证据" : "—";
    heading.append(node("strong", "", names[field.name] || field.name),
      groupBadge(field.status, {current:"组值", duplicate:"组值 · 重复印证", supplement:candidateLabel}[field.status]), values);
    if (needsMaterialReview(field)) heading.append(groupBadge("conflict", "附件有冲突"));
    row.append(heading);
    const entries = [...field.body_entries, ...field.attachment_entries];
    entries.forEach((entry, index) => {
      const mail = emails.get(entry.email_id), item = mail.body?.id === entry.material_id ? mail.body :
        mail.attachments.find((attachment) => attachment.id === entry.material_id);
      const line = node("div", "group-field-entry");
      line.append(node("p", "group-path", groupPath(mail, item)), groupBadge(entry.relationship),
        node("strong", "", resultText(entry.value, entry.unit || (field.name === "package_count" ? "package" : null))));
      if (entry.status === "conflict" && entry.relationship !== "conflict") line.append(groupBadge("conflict", "材料内冲突"));
      if (entry.via === "agent") line.append(node("span", "compare-help", "DeepSeek 候选 · 已校验"));
      if (entry.sources.length && item) {
        const evidence = node("div", "sources");
        entry.sources.forEach((source, sourceIndex) => evidence.append(groupSource(mail, item, source, `group-source-${field.name}-${index}-${sourceIndex}`)));
        line.append(evidence);
      } else line.append(node("p", "compare-help", reason(entry.reason) || (states[item?.state] ? `材料：${states[item.state]}` : "暂无正文来源。")));
      row.append(line);
    });
    (hideUnknown(field) ? unknown : body).append(row);
  }
  if (unknown.childElementCount > 1) body.append(unknown);
}

function renderLocalInquiryResult(content, result, emails) {
  const panel = node("section", "reading group-panel");
  const first = emails.find((mail) => mail.id === result.first_inquiry.email_ref_id);
  panel.append(node("h3", "", "结构化询盘结果"),
    node("p", "compare-help", first ? "首询已手动确认：" + (first.subject || first.filename) : "未确认首询；首询字段留空。"));
  if (result.first_inquiry.source_status) {
    const status = result.first_inquiry.source_status;
    const availability = {available_in_archive:"归档中有首询", not_available_in_archive:"归档未包含首询", unknown:"归档首询可用性未知"};
    const reading = {missing:"缺正文", unverified:"正文未核验", verified:"正文已核验"};
    panel.append(node("p", "compare-help", [availability[status.original_availability], reading[status.body_read_state],
      status.reason === "original_not_in_archive" ? "可继续核对回复或历史转引" : ""].filter(Boolean).join(" · ")));
  }
  const labels = {subject:"主题", from_addr:"发件邮箱", to_addrs:"收件邮箱", cc_addrs:"抄送邮箱",
    salesperson:"业务员", inquiry_created_at:"询盘创建时间", internal_reference:"内部需求单号",
    shipment_density:"整票比重", cargo_line_density:"货行比重", is_large_cargo:"是否大货",
    ctns:"纸箱数", shipment_actual_weight:"整票实重", shipment_gross_weight:"整票毛重",
    shipment_chargeable_weight:"整票计费重", shipment_volume:"整票体积",
    line_net_weight:"货行净重", line_gross_weight:"货行毛重", line_volume:"货行体积", destination:"目的地",
    departure_port:"起运机场"};
  function show(parent, label, fields, owner, id) {
    const section = node("details", "group-field"); section.id = id;
    section.append(node("summary", "", label));
    const {mail, material} = inquiryMaterial(emails, owner.email_ref_id, owner.material_id);
    const loc = owner.location || {};
    const place = [mail && groupPath(mail, material), loc.worksheet, loc.page && `第 ${loc.page} 页`,
      loc.table || loc.pointer, loc.row && `第 ${loc.row} 行`].filter(Boolean).join(" · ");
    if (place) section.append(node("p", "group-path", place));
    for (const [index, [name, field]] of Object.entries(fields).entries()) {
      const value = Array.isArray(field.value) ? (field.value.join("、") || "无") :
        field.value == null ? (field.status === "conflict" ? "冲突" : "未确定") : resultText(field.value, field.unit);
      const line = node("div", "group-field-entry");
      line.append(node("strong", "", `${labels[name] || names[name] || name}：${value}`));
      if (field.reason) line.append(node("p", "compare-help", pipelineReason(field.reason)));
      for (const [part, source] of (field.sources || []).entries()) {
        const origin = inquiryMaterial(emails, source.email_ref_id ?? owner.email_ref_id, source.material_id ?? owner.material_id);
        if (origin.mail && origin.material && (source.pointer || source.line || source.cell || source.page)) {
          line.append(groupSource(origin.mail, origin.material, source, `${id}-source-${index}-${part}`));
          if (origin.mail.id !== mail?.id) line.append(node("p", "compare-help", groupPath(origin.mail, origin.material)));
        } else if (source.excerpt) line.append(node("p", "compare-help", source.excerpt));
      }
      section.append(line);
    }
    parent.append(section);
    return section;
  }
  const objects = result.shipments || [], rows = result.cargo_lines || [];
  const roles = {historical_quote:"历史转引", first_inquiry:"首询", supplement:"同票补充", reference:"参考邮件"};
  const cargo = node("details", "group-unknown"); cargo.id = "structured-cargo";
  cargo.open = objects.length > 0 || rows.length > 0;
  cargo.append(node("summary", "", `独立货票 ${objects.length} · 货行 ${rows.length}`),
    node("p", "compare-help", "按货票、货行分别取证；历史转引保留原角色，汇总值不复制到货行。"));
  const byShipment = new Map();
  for (const row of rows) {
    if (!byShipment.has(row.shipment_id)) byShipment.set(row.shipment_id, []);
    byShipment.get(row.shipment_id).push(row);
  }
  const showRow = (parent, row) => show(parent,
    [roles[row.source_role], `货行 ${row.ordinal || row.location?.row || ""}`,row.direct_fields?.product_name?.value].filter(Boolean).join(" · "),
    {...row.direct_fields, ...row.fields}, row, "structured-row-" + row.object_id);
  for (const [i, shipment] of objects.entries()) {
    const linked = byShipment.get(shipment.object_id) || [];
    const detail = show(cargo, `${roles[shipment.role] || "来源待核"}货票 ${i + 1}${linked.length ? " · " + linked.length + " 行" : ""}`,
      shipment.direct_fields || {}, shipment, "structured-shipment-" + shipment.object_id);
    linked.forEach((row) => showRow(detail, row));
    byShipment.delete(shipment.object_id);
  }
  [...byShipment.values()].flat().forEach((row) => showRow(cargo, row));
  if (!objects.length && !rows.length) cargo.append(node("p", "compare-help", "当前材料没有可独立定位的货票或货行。"));
  panel.append(cargo);
  const other = node("details", "group-unknown"); other.id = "structured-inquiry-detail";
  other.append(node("summary", "", "首询汇总、邮件头与系统字段"));
  show(other, "首询汇总", {...result.shipment.fields, ...result.first_inquiry.direct_fields}, result.shipment, "structured-first");
  for (const [i, email] of result.emails.entries()) show(other,
    emails.find((mail) => mail.id === email.email_ref_id)?.filename || "邮件", email.fields, email, "structured-header-" + i);
  show(other, "业务系统", result.business_system, {}, "structured-system");
  panel.append(other);
  panel.append(button("导出结构化结果", () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], {type:"application/json"}));
    const link = document.createElement("a"); link.href = url; link.download = "inquiry-result.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }, "control"));
  content.append(panel);
}

function inquiryMaterial(emails, emailId, materialId) {
  const mail = emails.find((item) => item.id === emailId);
  return {mail, material: mail && (mail.body?.id === materialId ? mail.body : mail.attachments.find((item) => item.id === materialId))};
}

function renderObjectAudit(panel, audits, emails) {
  const outcomes = {unselected:"未选证", not_sent:"未发送", request_failed:"请求未完成",
    response_received:"已收到响应", no_candidate:"无候选", validation_rejected:"校验拒绝",
    corroborated:"同值印证", accepted:"新增采纳"};
  const details = node("details", "group-unknown"); details.id = "group-object-audit";
  const counts = new Map();
  details.append(node("summary", "", `按票／按行补提记录（${audits.length} 个对象）`));
  const summary = node("p", "compare-help"); details.append(summary);
  for (const audit of audits) {
    const {mail, material} = inquiryMaterial(emails, audit.email_id, audit.material_id);
    const row = node("div", "group-field-entry");
    row.append(node("strong", "", audit.kind === "shipment" ? "货票" : "货行"),
      node("p", "group-path", mail ? groupPath(mail, material) : "材料来源待核"));
    if (audit.fields?.length) row.append(node("p", "compare-help",
      "目标：" + audit.fields.map((name) => names[name] || name).join("、") + ` · ${audit.evidence?.length || 0} 处证据`));
    const card = $(`structured-${audit.kind === "shipment" ? "shipment" : "row"}-${audit.object_id}`);
    if (card) row.append(button("查看对象", () => {
      card.open = true;
      focusGroupResult(card.querySelector("summary").id ||= card.id + "-title");
    }, "control"));
    for (const event of audit.events?.length ? audit.events : [audit]) {
      const label = outcomes[event.outcome] || event.outcome;
      counts.set(label, (counts.get(label) || 0) + 1);
      row.append(node("p", "compare-help", [event.field && (names[event.field] || event.field), label,
        event.reason && pipelineReason(event.reason)].filter(Boolean).join(" · ")));
    }
    details.append(row);
  }
  summary.textContent = [...counts].map(([label, n]) => `${label} ${n}`).join(" · ");
  panel.append(details);
}

function renderGroupContextResult(panel, context, emails, structured) {
  const states = {complete:"已核对", no_gap:"没有可追加核对的缺项", no_evidence:"没有可发送的证据",
    request_limit:"本组请求额度已用完", timeout:"请求超时", service_failure:"模型服务未完成",
    invalid_response:"模型响应格式不符", local_failure:"本地校验未完成"};
  const labels = (fields) => fields.map((f) => names[f] || f).join("、");
  const section = node("details", "group-unknown");
  section.id = "group-context-result";
  section.open = context.supplements.length > 0;
  const timing = context.model_seconds == null ? "" : ` · 模型 ${context.model_seconds.toFixed(3)} 秒`;
  section.append(node("summary", "", `组级补提 · ${states[context.state] || context.state}`),
    node("p", "compare-help", `追加 ${context.requests || 0} 次请求${timing} · 采纳 ${context.supplements.length} · 拒绝 ${context.rejections?.length || 0}`));
  if (context.requested?.length) section.append(node("p", "compare-help", `已发送：${labels(context.requested)}（${context.fragments} 段）`));
  const skipped = (context.missing || []).filter((field) => !context.requested?.includes(field));
  if (skipped.length) section.append(node("p", "compare-help", `缺项未发送：${labels(skipped)}`));
  if (context.historical_candidates?.length) section.append(node("p", "compare-help",
    `${context.historical_candidates.length} 处历史引用待单独核对，不进入首询。`));
  if (context.rejections?.length) {
    const rejected = node("ul", "context-rejections");
    for (const item of context.rejections) rejected.append(node("li", "",
      `${item.name ? `${names[item.name] || item.name}：` : ""}${pipelineReason(item.reason)}`));
    section.append(rejected);
  }
  for (const [index, entry] of context.supplements.entries()) {
    const mail = emails.find((e) => e.id === entry.email_id);
    const item = mail?.body?.id === entry.material_id ? mail.body : mail?.attachments.find((a) => a.id === entry.material_id);
    const scope = structured?.supplements?.find((s) => s.email_ref_id === entry.email_id && s.material_id === entry.material_id);
    const row = node("div", "group-field-entry");
    row.append(node("strong", "", `${names[entry.name] || entry.name}：${resultText(entry.value, entry.unit)}`),
      groupBadge("supplement", scope?.role === "supplement" ? "仅作补充" : "参考：同票待核"));
    if (mail && item) {
      row.append(node("p", "group-path", groupPath(mail, item)));
      for (const [i, source] of entry.sources.entries()) row.append(groupSource(mail, item, source, `group-context-source-${index}-${i}`));
    }
    section.append(row);
  }
  panel.append(section);
}

function renderGroupRouteComparison(panel, comparison) {
  const box = node("details", "group-unknown");
  box.id = "group-route-comparison";
  box.append(node("summary", "", "同一份读取证据的路线对照（首轮）"),
    node("p", "group-field-counts",
      `组值变化 ${comparison.group_value_changes} 项 · 仅候选、证据或状态变化 ${comparison.other_changes} 项`),
    node("p", "compare-help", "对照的是输出差异，字段是否正确以原文或已核对标注为准。"));
  if (!comparison.changes.length) box.append(node("p", "compare-help", "两条路线的组值和候选无差异。"));
  const describe = (side, name) => {
    const format = (entry) => resultText(entry.value, entry.unit || (name === "package_count" ? "package" : null));
    if (side.value != null) return `${format(side)} · ${side.sources} 条来源`;
    const candidates = side.candidates.map((item) => `${item.scope === "attachment" ? "附件" : "正文"} ${format(item)}`);
    const state = {supplement:"待核对", conflict:"冲突", uncertain:"未确定"}[side.status] || side.status;
    return `${state}${candidates.length ? " · " + candidates.join(" / ") : ""} · ${side.sources} 条来源`;
  };
  for (const item of comparison.changes) {
    const row = node("div", "group-field-entry");
    row.append(node("strong", "", names[item.name] || item.name),
      node("p", "compare-help", `本地规则：${describe(item.rules, item.name)}`),
      node("p", "compare-help", `规则 + DeepSeek：${describe(item.combined, item.name)}`));
    const link = button("查看字段来源", () => {
      const field = $(`group-field-${item.name}`);
      if (!field) return;
      if (field.parentElement?.tagName === "DETAILS") field.parentElement.open = true;
      field.open = true;
      field.querySelector("summary").focus();
    }, "control");
    row.append(link);
    box.append(row);
  }
  panel.append(box);
}
