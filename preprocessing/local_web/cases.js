"use strict";

let demoLoading = false;

async function loadDemoCases() {
  const list = $("demo-cases-list");
  list.replaceChildren(node("p", "compare-help", "正在读取案例目录…"));
  try {
    const {cases} = await request("/api/demo-cases", {signal:AbortSignal.timeout(4000)});
    list.replaceChildren(...cases.map((demo) => {
      const card = node("article", "demo-case");
      const meta = node("div", "demo-meta");
      meta.append(...demo.formats.map((format) => node("span", "file-type", format)),
        node("span", "compare-help", `${demo.emails} 封邮件`));
      const action = button("打开案例", () => openDemoCase(demo), "control");
      action.setAttribute("aria-label", `打开案例：${demo.title}`);
      card.append(meta, node("h3", "", demo.title), node("p", "compare-help", demo.description), action);
      return card;
    }));
  } catch {
    list.replaceChildren(node("p", "compare-help", "暂时无法读取案例目录。"),
      button("重试", loadDemoCases, "control"));
  }
}

function openDemoLibrary() {
  $("demo-library").open = true;
  $("demo-library-title").focus({preventScroll:true});
  $("demo-library").scrollIntoView({block:"nearest"});
}

function registerDemoMail(id, demo, first, filename) {
  const key = `demo:${first}`, path = `内置案例/${demo.title}`;
  const group = groupCatalog.get(key) || {kind:"demo", label:demo.title, path,
    ids:new Set(), current:first, demo};
  group.ids.add(id);
  groupCatalog.set(key, group);
  groupPaths.set(id, `${path}/${filename}`);
}

function focusDemoWorkspace() {
  $("demo-library").open = false;
  $("workspace").scrollIntoView({block:"start"});
  $("group-title")?.focus({preventScroll:true});
}

async function openDemoCase(demo) {
  if (demoLoading) return;
  const existing = [...groupCatalog].find(([, item]) => item.demo?.id === demo.id && item.ids.size === demo.emails);
  if (existing) {
    selectionVersion += 1;
    view = "group";
    activateGroup(existing[0]);
    focusDemoWorkspace();
    return;
  }
  const round = roundVersion, status = $("demo-status");
  demoLoading = true;
  status.hidden = false;
  status.textContent = `正在导入「${demo.title}」…`;
  try {
    const {files} = await request(`/api/demo-cases/${demo.id}`, {signal:AbortSignal.timeout(10000)});
    if (round !== roundVersion) return;
    const ids = await enqueue(files.map(({filename, content}) =>
      new File([Uint8Array.from(atob(content), (char) => char.charCodeAt(0))], filename)), {demo});
    if (round !== roundVersion) return;
    if (ids?.length === demo.emails) focusDemoWorkspace();
    else {
      status.textContent = `已导入 ${ids?.length || 0}/${demo.emails} 封，可重试打开此案例。`;
      return;
    }
    status.hidden = true;
  } catch {
    status.textContent = "无法导入此案例，请检查本机连接后重试。";
  } finally {
    if (round !== roundVersion) status.hidden = true;
    demoLoading = false;
  }
}

function renderDemoHint(demo) {
  const hint = node("div", "demo-hint");
  hint.append(node("strong", "", "案例核对重点"), node("p", "compare-help", demo.check));
  return hint;
}
