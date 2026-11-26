// Фронтенд без сборки и зависимостей. Разделы:
//   api       запросы к /api (единственное место, где есть fetch)
//   settings  настройки проверки (localStorage) и ключи провайдеров (только память вкладки)
//   документы, проверки (очередь, остановка), результат, история, сравнение
// Маршрут в адресе: #/check, #/result/<id>, #/history, #/compare.
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (n) => Number(n || 0).toLocaleString("ru-RU");
// загруженные файлы хранятся с префиксом хэша (1e088fbcbbd8_имя.md): людям показываем только имя
const docName = (n) => String(n || "").replace(/^[0-9a-f]{12}_/, "");
const SEV = { critical: "Критично", major: "Существенно", minor: "Незначительно" };
const STATUS = { open: "открыто", closed: "закрыто ответом", changed: "изменено ответом" };
const JOB_STATUS = { queued: "В очереди", running: "Идёт", done: "Готово", error: "Ошибка", cancelled: "Остановлено" };

// ---------- api ----------

const api = {
  async call(method, path, body, isForm) {
    const headers = {};
    const key = state.keys[settings.provider()];
    if (key) headers["X-Api-Key"] = key;
    if (body && !isForm) headers["Content-Type"] = "application/json";
    const r = await fetch(path, { method, headers, body: isForm ? body : body && JSON.stringify(body) });
    const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
    if (!r.ok) throw new Error(data?.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `HTTP ${r.status}`);
    return data;
  },
  status: () => api.call("GET", "/api/status"),
  samples: () => api.call("GET", "/api/samples"),
  upload(file) { const f = new FormData(); f.append("file", file); return api.call("POST", "/api/documents", f, true); },
  document: (id) => api.call("GET", `/api/documents/${encodeURIComponent(id)}`),
  preview: (document, options) => api.call("POST", "/api/preview", { document, options }),
  startJob: (document, options) => api.call("POST", "/api/jobs", { document, options }),
  jobs: () => api.call("GET", "/api/jobs"),
  job: (id, from) => api.call("GET", `/api/jobs/${id}?log_from=${from}`),
  stopJob: (id) => api.call("POST", `/api/jobs/${id}/stop`),
  runs: () => api.call("GET", "/api/runs"),
  run: (id) => api.call("GET", `/api/runs/${encodeURIComponent(id)}`),
  reportHtml: (id) => api.call("GET", `/api/runs/${encodeURIComponent(id)}/report`),
  fileUrl: (id, kind) => `/api/runs/${encodeURIComponent(id)}/files/${kind}`,
  answer: (id, answers, options) => api.call("POST", `/api/runs/${encodeURIComponent(id)}/answers`, { answers, options }),
  assistant: (id, question, history, focus, options) => api.call("POST", `/api/runs/${encodeURIComponent(id)}/assistant`, { question, history, focus, options }),
  compare: (a, b) => api.call("GET", `/api/compare?before=${encodeURIComponent(a)}&after=${encodeURIComponent(b)}`),
  standards: () => api.call("GET", "/api/standards"),
  saveStandard: (id, body, create) => api.call("PUT", `/api/standards/${encodeURIComponent(id)}${create ? "?create=true" : ""}`, body),
  deleteStandard: (id) => api.call("DELETE", `/api/standards/${encodeURIComponent(id)}`),
  draftStandard: (text, options) => api.call("POST", "/api/standards/draft", { text, options }),
  draft: (id) => api.call("GET", `/api/runs/${encodeURIComponent(id)}/draft`),
  draftEdit: (id, edit) => api.call("POST", `/api/runs/${encodeURIComponent(id)}/draft/edit`, edit),
  draftSave: (id, text) => api.call("PUT", `/api/runs/${encodeURIComponent(id)}/draft`, { text }),
  draftUndo: (id) => api.call("POST", `/api/runs/${encodeURIComponent(id)}/draft/undo`),
  draftReset: (id) => api.call("POST", `/api/runs/${encodeURIComponent(id)}/draft/reset`),
  draftCheck: (id, options) => api.call("POST", `/api/runs/${encodeURIComponent(id)}/draft/check`, { options }),
};

// Название модели для людей: «gemini-3.6-flash» превращается в «Gemini 3.6 Flash», «claude-opus-5-5» в «Claude Opus 5.5».
// Соседние числа склеиваются в версию, дата выпуска в конце (20250929) отбрасывается. В настройках и отчёте остаётся точный идентификатор.
function modelLabel(id) {
  const parts = String(id || "").replace(/^models\//, "").split(/[-_/\s:]+/).filter(Boolean);
  const out = [];
  for (const p of parts) {
    if (/^\d{8}$/.test(p) || p === "latest") continue;
    const prev = out[out.length - 1];
    if (/^\d+$/.test(p) && prev && /^\d+(\.\d+)*$/.test(prev)) out[out.length - 1] = `${prev}.${p}`;
    else out.push(p);
  }
  return out.map((w) => (/^(gpt|glm|qwq)$/i.test(w) || /^[a-z]{1,2}$/i.test(w) ? w.toUpperCase()
    : /^[a-zа-я]/i.test(w) ? w[0].toUpperCase() + w.slice(1) : w)).join(" ");
}

// ---------- состояние и настройки ----------

const state = {
  status: null,         // /api/status: провайдеры, стандарты, значения по умолчанию
  keys: {},             // ключи по провайдерам: только в памяти вкладки
  docs: [],             // выбранные документы (ответы /api/documents)
  jobs: [],             // /api/jobs
  autoOpen: null,       // id проверки, результат которой открыть сразу после завершения
  openLogs: new Set(),  // проверки с раскрытым журналом
  logs: {},             // id -> {text, next}
  polling: false,
  run: null,            // JSON /api/runs/<id>
  filter: { sev: "", text: "", shown: 8 },
  chats: {},            // id проверки -> реплики помощника
};

const settings = {
  provider: () => $("#opt-provider").value || "gemini",
  providerInfo: (id) => state.status.providers.find((p) => p.id === id),
  saved: () => JSON.parse(localStorage.getItem("validator.options.v2") || "{}"),
  load() {
    const st = state.status, saved = settings.saved(), d = st.defaults;
    $("#opt-provider").innerHTML = st.providers.map((p) => `<option value="${esc(p.id)}">${esc(p.title)}</option>`).join("");
    $("#opt-provider").value = saved.provider || d.provider;
    settings.fillProvider(saved.perProvider?.[settings.provider()]);
    settings.renderStandards(saved.standards || d.standards);
    $("#opt-batch").value = saved.batch_size || d.batch_size;
    $("#opt-parallel").value = saved.parallel || d.parallel;
    $("#opt-redact").checked = saved.redact ?? d.redact;
    $("#opt-ner").checked = saved.redact_ner ?? d.redact_ner;
    $("#opt-cache").checked = saved.use_cache ?? d.use_cache;
    $("#opt-fix").checked = !!saved.fix;
  },
  fillProvider(saved) {
    const p = settings.providerInfo(settings.provider());
    $("#opt-model").value = saved?.model || p.model;
    $("#opt-fallbacks").value = (saved?.fallbacks || p.fallbacks).join(", ");
    $("#opt-base-url").value = saved?.base_url ?? p.base_url ?? "";
    $("#base-url-field").hidden = p.id !== "openai";
    $("#opt-key").value = state.keys[p.id] || "";
    settings.showKey();
  },
  showKey() {
    const p = settings.providerInfo(settings.provider());
    const own = !!state.keys[p.id];
    $("#key-status").textContent = own ? "Используется ключ из этого поля." : p.has_env_key ? "Ключ найден в .env." : "В .env ключа нет: впишите его здесь.";
  },
  renderStandards(chosen) {
    chosen ||= settings.saved().standards || state.status.defaults.standards;
    $("#opt-standards").innerHTML = state.status.standards.map((s) => `
      <label class="check std" title="${esc(s.description)}">
        <input type="checkbox" value="${esc(s.id)}" ${s.mandatory ? "checked disabled" : chosen.includes(s.id) ? "checked" : ""}>
        <span><b>${esc(s.title)}</b>${s.mandatory ? " (всегда)" : ""}${s.kind === "custom" ? " (свой)" : s.kind === "modified" ? " (изменён)" : ""}<br><span class="muted small">${esc(s.description)}</span></span>
      </label>`).join("");
  },
  read() {
    const provider = settings.provider();
    const o = {
      provider,
      model: $("#opt-model").value.trim() || null,
      fallbacks: $("#opt-fallbacks").value.split(",").map((s) => s.trim()).filter(Boolean),
      base_url: provider === "openai" ? ($("#opt-base-url").value.trim() || null) : null,
      standards: [...document.querySelectorAll("#opt-standards input:checked")].map((i) => i.value),
      batch_size: Number($("#opt-batch").value) || null,
      parallel: Number($("#opt-parallel").value) || null,
      redact: $("#opt-redact").checked,
      redact_ner: $("#opt-ner").checked,
      use_cache: $("#opt-cache").checked,
      fix: $("#opt-fix").checked,
    };
    const saved = settings.saved();
    const perProvider = { ...(saved.perProvider || {}), [provider]: { model: o.model, fallbacks: o.fallbacks, base_url: o.base_url } };
    localStorage.setItem("validator.options.v2", JSON.stringify({ ...o, perProvider }));
    return o;
  },
};

// ---------- общие куски разметки ----------

function table(rows, cols, cls = "") {
  if (!rows.length) return `<p class="muted">Нет данных</p>`;
  const head = cols.map(([, title]) => `<th>${esc(title)}</th>`).join("");
  const body = rows.map((r) => `<tr>${cols.map(([key, , fmt]) => `<td>${fmt ? fmt(r[key], r) : esc(r[key])}</td>`).join("")}</tr>`).join("");
  return `<table class="${cls}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

// строка показателей без рамок: [подпись, значение, необязательный класс critical|major|minor]
const figures = (items) => `<dl class="figures">${items.map(([label, value, cls]) => `<div class="${cls || ""}"><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>`;
const win = (title, body) => `<div class="box"><h2>${esc(title)}</h2>${body}</div>`;
const showError = (el, e) => { el.innerHTML = `<p class="error">${esc(e.message || e)}</p>`; };
const fmtTime = (s) => (s >= 60 ? `${Math.floor(s / 60)} мин ${Math.round(s % 60)} с` : `${Math.round(s)} с`);

// ---------- страница «Проверка»: документы ----------

function addDoc(info) {
  if (!state.docs.some((d) => d.id === info.id)) state.docs.push(info);
  renderDocs();
}

function renderDocs() {
  const el = $("#doc-list");
  $("#btn-run").disabled = !state.docs.length;
  $("#btn-run").textContent = state.docs.length > 1 ? `Проверить все (${state.docs.length})` : "Проверить";
  if (!state.docs.length) { el.innerHTML = ""; return; }
  el.innerHTML = table(state.docs, [
    ["name", "Документ", (v, d) => `<b>${esc(docName(v))}</b>${d.title && d.title !== docName(v) ? `<br><span class="muted">${esc(d.title)}</span>` : ""}`],
    ["id", "", (v) => `<button type="button" class="link" data-preview="${esc(v)}">Что уйдёт в модель</button> / <button type="button" class="link" data-remove="${esc(v)}">Убрать</button>`],
  ], "doclist");
  el.querySelectorAll("[data-preview]").forEach((b) => { b.onclick = () => onPreview(b.dataset.preview); });
  el.querySelectorAll("[data-remove]").forEach((b) => { b.onclick = () => { state.docs = state.docs.filter((d) => d.id !== b.dataset.remove); renderDocs(); }; });
}

async function addFiles(files) {
  for (const f of files) {
    try { addDoc(await api.upload(f)); } catch (e) { $("#preview").innerHTML = win("Ошибка загрузки", `<p class="error">${esc(f.name)}: ${esc(e.message)}</p>`); }
  }
}

// Файлы можно перетащить на блок «Документы» или вставить (Ctrl+V) на странице «Проверка».
// Вставленный текст без файла становится документом «вставленный_текст.md».
function setupDrop() {
  const zone = $("#drop-zone");
  let depth = 0;
  zone.addEventListener("dragenter", (e) => { if (e.dataTransfer?.types.includes("Files")) { e.preventDefault(); depth++; zone.classList.add("over"); } });
  zone.addEventListener("dragover", (e) => { if (e.dataTransfer?.types.includes("Files")) e.preventDefault(); });
  zone.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; zone.classList.remove("over"); } });
  zone.addEventListener("drop", (e) => {
    e.preventDefault(); depth = 0; zone.classList.remove("over");
    addFiles([...(e.dataTransfer?.files || [])]);
  });
  // файл, брошенный мимо блока, браузер иначе откроет вместо страницы
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());
  document.addEventListener("paste", (e) => {
    if ($("#view-check").hidden || e.target.closest?.("input, textarea, [contenteditable]")) return;
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) { e.preventDefault(); addFiles(files); return; }
    const text = e.clipboardData?.getData("text/plain") || "";
    if (text.trim().length >= 20) {
      e.preventDefault();
      addFiles([new File([text], "вставленный_текст.md", { type: "text/markdown" })]);
    }
  });
}

async function onPreview(docId) {
  const out = $("#preview"), doc = state.docs.find((d) => d.id === docId);
  const title = `Что уйдёт в модель: ${doc ? doc.name : ""}`;
  out.innerHTML = win(title, `<p class="muted">Маскирую локально, в модель ничего не уходит…</p>`);
  try {
    const p = await api.preview(docId, settings.read());
    out.innerHTML = win(title, (p.replacements.length
      ? `<p class="ok">Замаскировано значений: ${p.replacements.length}</p>` + table(p.replacements, [["placeholder", "Метка"], ["kind", "Тип"], ["value", "Исходное (частично)"]])
      : `<p>Чувствительных данных не найдено${p.enabled ? "" : " (маскирование выключено)"}</p>`)
      + `<h3>Документ, который увидит модель</h3>
         <p class="muted">Подсвечены замаскированные значения. <label><input type="checkbox" id="raw-toggle"> исходный текст</label></p>
         <iframe id="masked-frame" class="docframe" sandbox title="Маскированный документ"></iframe>
         <pre class="masked" id="masked-raw" hidden>${esc(p.masked)}</pre>`);
    $("#masked-frame").srcdoc = docPage(p.masked_html);
    $("#raw-toggle").onchange = (e) => { $("#masked-frame").hidden = e.target.checked; $("#masked-raw").hidden = !e.target.checked; };
  } catch (e) { out.innerHTML = win(title, `<p class="error">${esc(e.message || e)}</p>`); }
}

// ---------- проверки: запуск, очередь, остановка ----------

async function onRun() {
  const options = settings.read();
  const started = [];
  for (const d of state.docs) {
    try {
      const j = await api.startJob(d.id, options);
      started.push(j.id);
    } catch (e) { $("#preview").innerHTML = win("Не удалось запустить", `<p class="error">${esc(d.name)}: ${esc(e.message)}</p>`); }
  }
  state.autoOpen = started.length === 1 ? started[0] : null;
  if (started.length === 1) state.openLogs.add(started[0]);
  pollJobs();
}

async function onStop(id) {
  try { await api.stopJob(id); } catch (e) { /* задача уже завершилась */ }
  pollJobs();
}

async function pollJobs() {
  if (state.polling) return;
  state.polling = true;
  try {
    const prev = Object.fromEntries(state.jobs.map((j) => [j.id, j.status]));
    state.jobs = await api.jobs();
    for (const id of state.openLogs) await refreshLog(id);
    renderJobs();
    for (const j of state.jobs) {
      if (prev[j.id] && prev[j.id] !== "done" && j.status === "done" && state.autoOpen === j.id && location.hash.startsWith("#/check")) {
        state.autoOpen = null;
        location.hash = `#/result/${j.run_id}`;
      }
    }
  } catch (e) { /* сервер недоступен: попробуем снова */ }
  state.polling = false;
  if (state.jobs.some((j) => j.status === "queued" || j.status === "running")) setTimeout(pollJobs, 1500);
}

async function refreshLog(id) {
  const cur = state.logs[id] || { text: "", next: 0 };
  try {
    const j = await api.job(id, cur.next);
    state.logs[id] = { text: cur.text + (j.log.length ? j.log.join("\n") + "\n" : ""), next: j.log_next };
  } catch (e) { /* нет такой задачи */ }
}

// Строка состояния под вкладками: видна только пока идут проверки, на любой странице; клик ведёт к списку проверок.
function renderNewsbar() {
  const live = state.jobs.filter((j) => j.status === "running" || j.status === "queued");
  $("#newsbar").hidden = !live.length;
  if (!live.length) return;
  const running = live.filter((j) => j.status === "running");
  const queued = live.length - running.length;
  const pct = Math.round(100 * live.reduce((s, j) => s + (j.progress || 0), 0) / live.length);
  const one = running.length === 1 && !queued ? running[0] : null;
  $("#job-indicator").textContent = one
    ? `${docName(one.document)}: ${one.stage || "идёт"}${one.attempt > 1 ? `, попытка ${one.attempt}` : ""}`
    : `Проверок: ${running.length}` + (queued ? `, в очереди: ${queued}` : "");
  $("#news-bar").style.width = `${Math.max(4, pct)}%`;
  $("#news-pct").textContent = `${pct}%` + (one ? ` / ${fmtTime(one.elapsed)} / ${modelLabel(one.model)}` : "");
}

function renderJobs() {
  renderNewsbar();
  $("#jobs-box").hidden = !state.jobs.length;
  if (!state.jobs.length) return;
  $("#jobs").innerHTML = state.jobs.map((j) => {
    const live = j.status === "queued" || j.status === "running";
    const bar = { running: "progress", done: "progress done", queued: "progress queued" }[j.status] || "progress failed";
    const log = state.logs[j.id];
    return `<div class="job ${esc(j.status)}">
      <div class="job-head">
        <b>${esc(docName(j.document))}</b>
        <span class="job-status">${esc(JOB_STATUS[j.status] || j.status)}${j.stop_requested && live ? " (останавливается)" : ""}${j.elapsed ? ` / ${fmtTime(j.elapsed)}` : ""}${j.attempt > 1 ? ` / попытка ${j.attempt}` : ""}</span>
        <span class="job-actions">
          ${live ? `<button type="button" class="btn small" data-stop="${esc(j.id)}" ${j.stop_requested ? "disabled" : ""}>Остановить</button>` : ""}
          ${j.status === "done" ? `<a class="btn small primary" href="#/result/${esc(j.run_id)}">Открыть результат</a>` : ""}
          ${j.status === "error" || j.status === "cancelled" ? `<button type="button" class="btn small" data-jretry="${esc(j.id)}">Повторить</button>` : ""}
          <button type="button" class="link" data-log="${esc(j.id)}">${state.openLogs.has(j.id) ? "Скрыть журнал" : "Журнал"}</button>
        </span>
      </div>
      <div class="${bar}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(j.progress * 100)}"><span style="width:${live ? Math.max(4, Math.round(j.progress * 100)) : 100}%"></span></div>
      <p class="muted small job-last">${live && j.stage ? `${esc(j.stage)} / ${Math.round(j.progress * 100)}% / ` : ""}${esc(j.error || j.last)}</p>
      ${state.openLogs.has(j.id) ? `<pre class="log">${esc(log ? log.text : "…")}</pre>` : ""}
    </div>`;
  }).join("");
  document.querySelectorAll("[data-stop]").forEach((b) => { b.onclick = () => onStop(b.dataset.stop); });
  document.querySelectorAll("[data-jretry]").forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      try { await api.call("POST", `/api/jobs/${b.dataset.jretry}/retry`); } catch (e) { $("#preview").innerHTML = win("Не удалось повторить", `<p class="error">${esc(e.message)}</p>`); }
      pollJobs();
    };
  });
  document.querySelectorAll("[data-log]").forEach((b) => {
    b.onclick = async () => {
      const id = b.dataset.log;
      if (state.openLogs.has(id)) state.openLogs.delete(id); else { state.openLogs.add(id); await refreshLog(id); }
      renderJobs();
    };
  });
  document.querySelectorAll("pre.log").forEach((p) => { p.scrollTop = p.scrollHeight; });
}

// ---------- страница «Результат» ----------
// Одна страница вместо вкладок: вердикт, нарушения карточками, вопросы, подробности в раскрывающихся блоках.

// HTML документа или отчёта показываем только в iframe с sandbox (без скриптов), со своими стилями
const docPage = (html) => `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="${location.origin}/static/doc.css"><body>${html}</body>`;
const SEV_ORDER = ["critical", "major", "minor"];
const PAGE = 8;  // сколько нарушений показывать сразу
const firstSentence = (s) => { const m = String(s || "").match(/^.+?[.!?](\s|$)/); return (m ? m[0] : String(s || "")).trim(); };
// цитата документа: ячейки таблиц из PDF приходят с «|», показываем их через точку
const quote = (a) => `<blockquote>${esc(a.text.replace(/\s*\|\s*/g, " / ").replace(/^[\s/]+|[\s/]+$/g, ""))}${a.where ? `<cite>${esc(a.where)}</cite>` : ""}</blockquote>`;
const sevTag = (sev) => `<span class="sev sev-${esc(sev)}">${esc(SEV[sev] || sev)}</span>`;

async function showResult(runId) {
  if (!runId) runId = state.run?.id;
  $("#res-empty").hidden = !!runId;
  $("#res-body").hidden = !runId;
  if (!runId) { assistant.close(); return; }
  if (state.run?.id !== runId) {
    $("#res-title").textContent = "Загружаю…";
    try { state.run = await api.run(runId); } catch (e) { showError($("#res-meta"), e); return; }
    state.filter = { sev: "", text: "", shown: PAGE };
    document.querySelectorAll("details.more").forEach((d) => { d.open = false; });
    assistant.reset();
  }
  const r = state.run;
  $("#res-title").textContent = r.document.title || docName(r.document.name);
  $("#res-meta").textContent = `${docName(r.document.name)} / проверка от ${new Date(r.started).toLocaleString("ru-RU")}`
    + (r.elapsed ? ` / ${fmtTime(r.elapsed)}` : "") + ` / ${r.standards.map((x) => x.title).join(", ")}`;
  const files = [["report", "отчёт"], ["questions", "вопросы"], ...(r.has_fixed ? [["fixed", "исправленная редакция"]] : []), ["result", "структурированный результат"]];
  $("#res-files").innerHTML = "Скачать: " + files.map(([k, l]) => `<a href="${api.fileUrl(r.id, k)}">${esc(l)}</a>`).join(" / ");
  renderVerdict(r); renderSevFilter(r); renderFindings(r); renderQuestions(r); renderMore(r);
  if (draft.runId !== r.id) { $("#draft-editor").hidden = true; $("#draft-out").innerHTML = ""; await draft.load(); }
}

function verdictWord(text) {
  const t = String(text || "").toUpperCase();
  if (t.startsWith("НЕЛЬЗЯ")) return ["stop", "нельзя"];
  if (t.includes("ОГОВОРК")) return ["warn", "с оговорками"];
  return ["go", "можно"];
}

function renderVerdict(r) {
  const s = r.summary, d = r.decision;
  const [cls, word] = verdictWord(d.development);
  const [tcls, tword] = verdictWord(d.testing);
  const fix = d.fix_first || [];
  $("#res-verdict").className = `verdict box ${cls}`;
  $("#res-verdict").innerHTML = `
    <div class="verdict-main">
      <p class="verdict-line">Разработку начинать <b>${esc(word)}</b>. Тесты проектировать <b class="${tcls}">${esc(tword)}</b>.</p>
      <p class="muted">${esc(d.development_rationale || d.development)}</p>
      ${figures([["открытых нарушений", s.open], ["критичных", s.critical, "critical"], ["существенных", s.major, "major"],
                 ["незначительных", s.minor, "minor"], ["вопросов", s.questions], ...(s.closed ? [["закрыто ответами", s.closed]] : [])])}
    </div>
    ${fix.length ? `<div class="verdict-fix">
      <h3>Исправить в первую очередь</h3>
      <ol>${fix.slice(0, 3).map((x) => `<li><b>${esc(x.title)}.</b> ${esc(x.action)} <span class="muted">${esc(x.finding_ids.join(", "))}</span></li>`).join("")}</ol>
      ${fix.length > 3 ? `<details class="inline"><summary>Ещё ${fix.length - 3}</summary><ol start="4">${fix.slice(3).map((x) => `<li><b>${esc(x.title)}.</b> ${esc(x.action)} <span class="muted">${esc(x.finding_ids.join(", "))}</span></li>`).join("")}</ol></details>` : ""}
    </div>` : ""}`;
}

function renderSevFilter(r) {
  const open = r.findings.filter((f) => f.status !== "closed");
  const n = (sev) => open.filter((f) => !sev || f.severity === sev).length;
  const items = [["", "Все"], ["critical", "Критичные"], ["major", "Существенные"], ["minor", "Незначительные"]];
  $("#f-sev").innerHTML = items.map(([v, l]) =>
    `<button type="button" data-sev="${v}" aria-pressed="${state.filter.sev === v}" class="${state.filter.sev === v ? "on" : ""}">${l} <span class="count">${n(v)}</span></button>`).join("");
  $("#f-sev").querySelectorAll("button").forEach((b) => {
    b.onclick = () => { state.filter.sev = b.dataset.sev; state.filter.shown = PAGE; renderSevFilter(r); renderFindings(r); };
  });
}

function renderFindings(r) {
  const { sev, text } = state.filter;
  const q = text.toLowerCase();
  const rows = r.findings
    .filter((f) => (!sev || f.severity === sev) && (!q || `${f.id} ${f.problem} ${f.fix} ${f.criterion_name} ${f.req_ids.join(" ")}`.toLowerCase().includes(q)))
    .sort((a, b) => (a.status === "closed") - (b.status === "closed") || SEV_ORDER.indexOf(a.severity) - SEV_ORDER.indexOf(b.severity));
  const shown = rows.slice(0, state.filter.shown);
  const el = $("#findings-list");
  if (!rows.length) { el.innerHTML = `<p class="muted">Ничего не найдено.</p>`; return; }
  el.innerHTML = shown.map((f) => {
    const where = f.anchors[0] ? f.anchors[0].where.replace(/, раздел.*$/, "") : "";
    return `<details class="card finding ${f.status === "closed" ? "closed" : ""}" data-fid="${esc(f.id)}">
      <summary>
        ${sevTag(f.severity)}
        <span class="card-title">${esc(firstSentence(f.problem))}</span>
        <span class="card-meta">${esc(f.id)}${f.req_ids.length ? " / " + esc(f.req_ids.join(", ")) : ""}${where ? " / " + esc(where) : ""}${f.status !== "open" ? " / " + esc(STATUS[f.status]) : ""}</span>
      </summary>
      <div class="card-body">
        ${f.anchors.slice(0, 2).map(quote).join("")}
        ${f.anchors.length > 2 ? `<details class="inline"><summary>Ещё цитаты: ${f.anchors.length - 2}</summary>${f.anchors.slice(2).map(quote).join("")}</details>` : ""}
        <dl class="kv">
          <dt>Проблема</dt><dd>${esc(f.problem)}</dd>
          <dt>Как исправить</dt><dd>${esc(f.fix)}</dd>
          <dt>Критерий</dt><dd>${esc(f.criterion_name)}</dd>
          ${f.resolution_note ? `<dt>Ответ заказчика</dt><dd>${esc(f.resolution_note)}</dd>` : ""}
        </dl>
        <p class="card-actions">
          <button type="button" class="btn small" data-ask="explain">Объяснить</button>
          <button type="button" class="btn small" data-ask="rewrite">Предложить формулировку</button>
          <button type="button" class="btn small" data-ask="split">Разбить на требования</button>
        </p>
      </div>
    </details>`;
  }).join("")
    + (rows.length > shown.length ? `<p class="more-row"><button type="button" class="btn" id="findings-more">Показать ещё ${Math.min(PAGE, rows.length - shown.length)} из ${rows.length - shown.length}</button></p>` : "");
  const more = $("#findings-more");
  if (more) more.onclick = () => { state.filter.shown += PAGE; renderFindings(r); };
  el.querySelectorAll("[data-ask]").forEach((b) => {
    b.onclick = () => {
      const fid = b.closest("[data-fid]").dataset.fid;
      assistant.ask(QUICK[b.dataset.ask], { type: "finding", id: fid, label: `${fid}: ${firstSentence(r.findings.find((f) => f.id === fid).problem)}` });
    };
  });
}

function renderQuestions(r) {
  const el = $("#questions-list");
  $("#questions-section").hidden = !r.questions.length;
  if (!r.questions.length) return;
  const prev = Object.fromEntries(r.answers.map((a) => [a.id, a.answer]));
  el.innerHTML = r.questions.map((q, i) => {
    const opts = q.options.map((o, k) => `Вариант ${k + 1}: ${o}`);
    const own = prev[q.id] && !opts.includes(prev[q.id]) ? prev[q.id] : "";
    const answered = !!prev[q.id];
    return `<details class="card question" data-qid="${esc(q.id)}" ${i === 0 && !answered ? "open" : ""}>
      <summary>
        <span class="tag ${q.priority === "blocking" ? "tag-strong" : ""}">${esc(q.priority_label)}</span>
        <span class="card-title">${esc(q.question)}</span>
        <span class="card-meta">${esc(q.addressee)}${answered ? " / отвечено" : ""}</span>
      </summary>
      <div class="card-body">
        ${q.quote ? quote({ text: q.quote.slice(0, 300), where: "" }) : ""}
        <p class="muted">Почему важно: ${esc(q.why)}</p>
        <fieldset class="options"><legend>Ответ</legend>
          ${opts.map((o) => `<label><input type="radio" name="q-${esc(q.id)}" value="${esc(o)}" ${prev[q.id] === o ? "checked" : ""}> ${esc(o)}</label>`).join("")}
          <label><input type="radio" name="q-${esc(q.id)}" value="__own" ${own ? "checked" : ""}> Свой ответ: <input class="own" value="${esc(own)}" aria-label="Свой ответ"></label>
        </fieldset>
        <p class="card-actions"><button type="button" class="btn small" data-askq="${esc(q.id)}">Черновик ответа от помощника</button></p>
      </div>
    </details>`;
  }).join("")
    + `<div class="row actions"><label class="check"><input id="ans-fix" type="checkbox" checked> Сформировать исправленную редакцию</label>
       <button id="btn-answers-ai" class="btn" type="button">Черновики ответов от помощника</button>
       <button id="btn-answers" class="btn primary" type="button">Перепроверить с ответами</button></div><div id="answers-out"></div>`;
  $("#btn-answers").onclick = onAnswers;
  $("#btn-answers-ai").onclick = () => assistant.ask(QUICK.answers_all, null);
  el.querySelectorAll("[data-askq]").forEach((b) => {
    b.onclick = () => assistant.ask(QUICK.answer_one,
      { type: "question", id: b.dataset.askq, label: `${b.dataset.askq}: ${r.questions[Number(b.dataset.askq.replace(/\D/g, "")) - 1].question}` });
  });
}

// Черновик ответа от помощника: выбрать совпадающий вариант или вписать свой ответ. Отправляет ответы только пользователь.
function fillAnswer(qid, text) {
  const box = document.querySelector(`.question[data-qid="${CSS.escape(qid)}"]`);
  if (!box) return false;
  const norm = (s) => s.toLowerCase().replace(/[«»"".,:;]/g, "").replace(/\s+/g, " ").trim();
  const radios = [...box.querySelectorAll("input[type=radio]")];
  const num = text.match(/^Вариант\s+(\d+)/i);
  const hit = radios.find((r) => r.value !== "__own" && (norm(r.value) === norm(text) || (num && r.value.startsWith(`Вариант ${num[1]}:`))));
  if (hit) hit.checked = true;
  else { radios.find((r) => r.value === "__own").checked = true; box.querySelector(".own").value = text; }
  box.classList.add("drafted");
  const meta = box.querySelector(".card-meta");
  if (!meta.textContent.includes("черновик помощника")) meta.textContent += " / черновик помощника";
  box.open = true;
  return true;
}

async function onAnswers() {
  const answers = {};
  document.querySelectorAll(".question").forEach((box) => {
    const picked = box.querySelector("input[type=radio]:checked");
    if (!picked) return;
    answers[box.dataset.qid] = picked.value === "__own" ? box.querySelector(".own").value : picked.value;
  });
  try {
    const opts = { ...settings.read(), fix: $("#ans-fix").checked, standards: null };  // стандарты как у исходной проверки
    const j = await api.answer(state.run.id, answers, opts);
    state.autoOpen = j.id; state.openLogs.add(j.id);
    location.hash = "#/check";
    pollJobs();
  } catch (e) { showError($("#answers-out"), e); }
}

function renderMore(r) {
  $("#c-req").textContent = r.requirements.length;
  $("#c-check").textContent = r.checklist.length ? `${r.checklist.filter((c) => c.answer.toLowerCase() === "да").length} из ${r.checklist.length}` : "нет";
  $("#c-cov").textContent = r.coverage.length ? `без критериев: ${r.coverage.filter((c) => !c.acceptance_ids.length).length} из ${r.coverage.length}` : "";
  $("#more-requirements").innerHTML = table(r.requirements, [
    ["id", "ID"], ["doc_id", "В документе"], ["summary", "Суть"], ["where", "Где"],
    ["open_findings", "Нарушений", (v, q) => (v ? `<button type="button" class="link" data-req="${esc(q.id)}">${v}</button>` : "")],
  ]);
  $("#more-requirements").querySelectorAll("[data-req]").forEach((b) => {
    b.onclick = () => { state.filter = { sev: "", text: b.dataset.req, shown: 50 }; $("#f-text").value = b.dataset.req; renderSevFilter(r); renderFindings(r); $("#h-findings").scrollIntoView({ behavior: "smooth" }); };
  });
  $("#more-checklist").innerHTML = table(r.checklist, [["item", "Пункт"], ["answer", "Ответ"], ["rationale", "Обоснование"]]);
  $("#more-coverage").innerHTML = table(r.coverage, [["req_id", "Требование"], ["acceptance_ids", "Критерии приёмки", (v) => esc(v.join(", ") || "нет")], ["missing_scenarios", "Не покрыто", (v) => esc(v.join("; "))]]);
  const SET = { ok: "соответствует", partial: "частично", violation: "нарушено" };
  $("#more-set").innerHTML = table(r.set_verdicts, [["criterion_name", "Критерий"], ["verdict", "Оценка", (v) => esc(SET[v] || v)], ["comment", "Комментарий"]]);
  const p = r.passport, m = Object.entries(p.masking);
  $("#more-passport").innerHTML = `<dl class="kv">
      <dt>Документ</dt><dd>${esc(docName(r.document.name))}, SHA-256 ${esc(r.document.sha256.slice(0, 16))}…</dd>
      <dt>Стандарты</dt><dd>${esc(r.standards.map((x) => x.title).join(", "))}</dd>
      <dt>Модель</dt><dd>${esc(modelLabel(p.model))} (${esc(settings.providerInfo(p.provider)?.title || p.provider)}); запросов ${p.calls}, из кэша ${p.cached}${[...new Set(p.steps.map((s) => s.model))].filter((m) => m !== p.model).length ? `; запасные: ${esc([...new Set(p.steps.map((s) => s.model))].filter((m) => m !== p.model).map(modelLabel).join(", "))}` : ""}</dd>
      <dt>Агент</dt><dd>${esc(r.agent_version)}</dd>
      <dt>Маскирование</dt><dd>${m.length ? esc(m.map(([k, v]) => `${k}: ${v}`).join(", ")) : "чувствительных данных не найдено"}</dd>
    </dl>` + p.warnings.map((w) => `<p class="error">${esc(w)}</p>`).join("");
}

// ---------- ИИ-помощник ----------

const QUICK = {
  explain: "Объясни простыми словами, в чём проблема и чем она грозит.",
  rewrite: "Исправь формулировку требования в документе: верни правку, которая устраняет это нарушение.",
  split: "Разбей это требование на атомарные проверяемые требования и верни правку документа.",
  tests: "Какие тест-кейсы нужны, чтобы проверить это требование?",
  question: "Какой вопрос задать заказчику, чтобы закрыть этот дефект?",
  answer_one: "Предложи черновик ответа на этот вопрос заказчику: какой вариант выбрать и почему, со ссылкой на документ.",
  answers_all: "Предложи черновики ответов на все вопросы заказчику. Где документ не даёт ответа, отметь это как допущение.",
  fix_all: "Исправь в документе критичные нарушения: верни правки рабочей редакции.",
};

const QUICK_LABEL = { explain: "Объяснить", rewrite: "Исправить в документе", split: "Разбить", tests: "Тест-кейсы", question: "Вопрос заказчику",
  answers_all: "Черновики ответов", fix_all: "Исправить критичные" };

const assistant = {
  focus: null,
  busy: false,
  chat: () => (state.chats[state.run?.id] ||= []),
  open() { $("#assistant").hidden = false; document.body.classList.add("with-assistant"); assistant.render(); $("#ask-input").focus(); },
  close() { $("#assistant").hidden = true; document.body.classList.remove("with-assistant"); },
  reset() { assistant.focus = null; assistant.render(); },
  setFocus(f) { assistant.focus = f; assistant.render(); },
  render() {
    const fc = $("#assistant-focus");
    fc.hidden = !assistant.focus;
    if (assistant.focus) {
      fc.innerHTML = `<span>Контекст: ${esc(assistant.focus.label)}</span><button type="button" class="link" id="focus-clear" aria-label="Убрать контекст">убрать</button>`;
      $("#focus-clear").onclick = () => assistant.setFocus(null);
    }
    const msgs = assistant.chat();
    $("#chat").innerHTML = (msgs.length ? "" : `<p class="muted chat-empty">Помощник объясняет нарушения, предлагает правки документа и черновики ответов заказчику. Правки попадают в рабочую редакцию только по вашей кнопке.</p>`)
      + msgs.map((m, i) => m.role === "user"
        ? `<div class="msg user">${esc(m.content)}</div>`
        : `<div class="msg bot ${m.error ? "err" : ""}">${esc(m.content)}${m.suggested ? `<div class="suggested"><pre>${esc(m.suggested)}</pre><button type="button" class="btn small" data-copy="${i}">Скопировать</button></div>` : ""}${assistant.actions(m, i)}${m.model ? `<span class="msg-meta">${esc(modelLabel(m.model))}</span>` : ""}</div>`).join("")
      + (assistant.busy ? `<div class="msg bot thinking" role="status">Помощник думает<span class="dots"></span></div>` : "");
    $("#chat").querySelectorAll("[data-copy]").forEach((b) => {
      b.onclick = async () => { await navigator.clipboard.writeText(msgs[Number(b.dataset.copy)].suggested); b.textContent = "Скопировано"; };
    });
    $("#chat").querySelectorAll("[data-apply]").forEach((b) => { b.onclick = () => assistant.applyEdit(...b.dataset.apply.split(":").map(Number)); });
    $("#chat").querySelectorAll("[data-apply-all]").forEach((b) => { b.onclick = () => assistant.applyAll(Number(b.dataset.applyAll)); });
    $("#chat").querySelectorAll("[data-fill]").forEach((b) => { b.onclick = () => assistant.fill(...b.dataset.fill.split(":").map(Number)); });
    $("#chat").querySelectorAll("[data-fill-all]").forEach((b) => { b.onclick = () => assistant.fillAll(Number(b.dataset.fillAll)); });
    $("#chat").scrollTop = $("#chat").scrollHeight;
    const quick = assistant.focus?.type === "finding" ? ["explain", "rewrite", "split", "tests", "question"]
      : assistant.focus?.type === "question" ? ["explain"] : ["explain", "fix_all", "answers_all"];
    $("#quick").innerHTML = quick.map((k) => `<button type="button" class="chip" data-quick="${k}" title="${esc(QUICK[k])}">${esc(QUICK_LABEL[k])}</button>`).join("");
    $("#quick").querySelectorAll("[data-quick]").forEach((b) => { b.onclick = () => assistant.ask(QUICK[b.dataset.quick], assistant.focus); });
    $("#ask-send").disabled = assistant.busy;
  },
  // Предложенные правки документа и черновики ответов: применяются только по кнопке.
  actions(m, i) {
    let html = "";
    if (m.edits?.length) {
      html += `<div class="actions-block"><p class="actions-title">Правки документа: ${m.edits.length}</p>`
        + m.edits.map((e, j) => `<div class="action ${e.applied ? "done" : ""}">
            <p class="action-head">${e.old_text ? `Строка ${e.line}` : `Вставка после строки ${e.line}`}${e.reason ? `: ${esc(e.reason)}` : ""}</p>
            ${e.old_text ? `<del>${esc(e.old_text)}</del>` : ""}<ins>${esc(e.new_text)}</ins>
            ${e.applied ? `<p class="ok small">Применено к рабочей редакции</p>`
              : e.ok === false ? `<p class="error small">Фрагмент не найден в текущей редакции</p>`
              : `<button type="button" class="btn small" data-apply="${i}:${j}">Применить</button>`}
            ${e.error ? `<p class="error small">${esc(e.error)}</p>` : ""}
          </div>`).join("")
        + (m.edits.filter((e) => !e.applied && e.ok !== false).length > 1 ? `<button type="button" class="btn small primary" data-apply-all="${i}">Применить все</button>` : "")
        + `</div>`;
    }
    if (m.answers?.length) {
      const qs = state.run?.questions || [];
      html += `<div class="actions-block"><p class="actions-title">Черновики ответов заказчику</p>`
        + m.answers.map((a, j) => `<div class="action ${a.filled ? "done" : ""}">
            <p class="action-head">${esc(a.question_id)}${qs.find((q) => q.id === a.question_id) ? `: ${esc(qs.find((q) => q.id === a.question_id).question)}` : ""}</p>
            <p>${esc(a.answer)}</p><p class="muted small">Основание: ${esc(a.basis)}</p>
            ${a.filled ? `<p class="ok small">Вписано в форму ответов</p>` : `<button type="button" class="btn small" data-fill="${i}:${j}">Вписать в ответ</button>`}
          </div>`).join("")
        + (m.answers.filter((a) => !a.filled).length > 1 ? `<button type="button" class="btn small primary" data-fill-all="${i}">Вписать все</button>` : "")
        + `<p class="muted small">Это черновик: перед перепроверкой подтвердите ответы у заказчика.</p></div>`;
    }
    return html;
  },
  async applyEdit(i, j) {
    const e = assistant.chat()[i].edits[j];
    try {
      draft.view = await api.draftEdit(state.run.id, { old_text: e.old_text, new_text: e.new_text, line: e.line, reason: e.reason });
      e.applied = true; e.error = "";
      draft.render();
    } catch (err) { e.error = err.message; }
    assistant.render();
  },
  async applyAll(i) {
    for (const [j, e] of assistant.chat()[i].edits.entries()) if (!e.applied && e.ok !== false) await assistant.applyEdit(i, j);
  },
  fill(i, j) {
    const a = assistant.chat()[i].answers[j];
    a.filled = fillAnswer(a.question_id, a.answer);
    assistant.render();
  },
  fillAll(i) {
    assistant.chat()[i].answers.forEach((a, j) => { if (!a.filled) assistant.fill(i, j); });
    $("#h-questions").scrollIntoView({ behavior: "smooth" });
  },
  async ask(question, focus) {
    if (!state.run || assistant.busy || !question.trim()) return;
    if (focus !== undefined) assistant.focus = focus;
    if ($("#assistant").hidden) assistant.open();
    const msgs = assistant.chat();
    const history = msgs.filter((m) => !m.error).map((m) => ({ role: m.role, content: m.content }));
    msgs.push({ role: "user", content: question });
    assistant.busy = true; assistant.render();
    try {
      const f = assistant.focus ? { type: assistant.focus.type, id: assistant.focus.id } : null;
      const out = await api.assistant(state.run.id, question, history, f, settings.read());
      msgs.push({ role: "assistant", content: out.answer, suggested: out.suggested_text, model: out.model,
        edits: out.edits || [], answers: out.answers || [] });
    } catch (e) {
      msgs.push({ role: "assistant", content: e.message || String(e), error: true });
    }
    assistant.busy = false; assistant.render();
  },
};

// ---------- рабочая редакция документа ----------
// Копия документа на сервере: правки помощника, правка вручную, отмена, проверка редакции как нового документа.

const KIND_RU = { edit: "правка помощника", manual: "вручную", reset: "возврат к исходному" };

const draft = {
  view: null,
  runId: null,
  async load() {
    draft.view = null;
    draft.runId = state.run.id;
    try { draft.view = await api.draft(state.run.id); } catch (e) { showError($("#draft-out"), e); }
    draft.render();
  },
  render() {
    const v = draft.view;
    if (!v) return;
    const n = v.edits.filter((e) => e.kind !== "reset").length;
    $("#draft-status").textContent = v.changed
      ? `Редакция отличается от исходного документа: правок ${n}. Исходный файл не меняется.`
      : "Правок пока нет. Попросите помощника исправить нарушение или поправьте текст вручную; исходный файл не меняется.";
    $("#draft-undo").disabled = !v.can_undo;
    $("#draft-reset").hidden = !v.changed;
    $("#draft-check").disabled = !v.changed;
    $("#draft-download").href = `/api/runs/${encodeURIComponent(state.run.id)}/draft/file`;
    $("#c-draft").textContent = v.edits.length || "";
    $("#draft-log-box").hidden = !v.edits.length;
    $("#draft-log").innerHTML = v.edits.slice().reverse().map((e) => `<div class="action">
        <p class="action-head">${esc(new Date(e.at).toLocaleString("ru-RU"))} / ${esc(KIND_RU[e.kind] || e.kind)}${e.reason && e.kind === "edit" ? `: ${esc(e.reason)}` : ""}</p>
        ${e.old ? `<del>${esc(e.old.slice(0, 600))}</del>` : ""}${e.new ? `<ins>${esc(e.new.slice(0, 600))}</ins>` : ""}
      </div>`).join("");
  },
  async run(fn, msg) {
    $("#draft-out").innerHTML = "";
    try { draft.view = await fn(); draft.render(); if (msg) $("#draft-out").innerHTML = `<p class="ok small">${esc(msg)}</p>`; }
    catch (e) { showError($("#draft-out"), e); }
  },
  edit() { $("#draft-text").value = draft.view?.text || ""; $("#draft-editor").hidden = false; $("#draft-text").focus(); },
  async save() {
    await draft.run(() => api.draftSave(state.run.id, $("#draft-text").value), "Сохранено");
    $("#draft-editor").hidden = true;
  },
  async check() {
    try {
      const j = await api.draftCheck(state.run.id, { ...settings.read(), standards: null, fix: false });
      state.comparePair = { before: state.run.id, job: j.id };
      state.openLogs.add(j.id);
      $("#draft-out").innerHTML = `<p class="ok small">Редакция поставлена на проверку. Когда она закончится, сравните результаты на странице «До и после».</p>`;
      pollJobs();
    } catch (e) { showError($("#draft-out"), e); }
  },
};

// ---------- страница «Стандарты» ----------
// Список стандартов с критериями, правка (встроенный сохраняется как изменённая копия), свои стандарты, импорт и черновик от ИИ.

const KIND_SHORT = { modified: "изменён", custom: "свой" };
const SCOPE_LABEL = { requirement: "каждое требование", set: "набор целиком" };

const stdPage = {
  list: [],
  editing: null,   // {data, isNew, note}
  async show() {
    try { stdPage.list = await api.standards(); } catch (e) { showError($("#std-list"), e); return; }
    state.status.standards = stdPage.list;
    settings.renderStandards();
    stdPage.renderList();
    stdPage.renderEditor();
  },
  renderList() {
    $("#std-list").innerHTML = stdPage.list.map((s) => `<details class="card std-card" data-sid="${esc(s.id)}">
      <summary>
        <span class="card-title">${esc(s.title)}</span>
        <span class="card-meta">${s.criteria.length} ${plural(s.criteria.length, "критерий", "критерия", "критериев")}${s.mandatory ? " / обязательный" : ""}${s.default ? " / по умолчанию" : ""}${s.kind !== "builtin" ? ` / <b>${esc(KIND_SHORT[s.kind])}</b>` : ""}</span>
      </summary>
      <div class="card-body">
        <p>${esc(s.description)}</p>
        <p class="muted small">В отчёте: ${esc(s.cite)}</p>
        ${table(s.criteria, [
          ["name", "Критерий", (v, c) => `<b>${esc(v || "(дополняет базовый)")}</b><br><span class="muted small">${esc(c.code)}</span>`],
          ["scope", "Что проверяет", (v, c) => `<span class="muted small">${esc(SCOPE_LABEL[v])}</span><br>${esc(c.question)}`],
          ["source", "Пункт"],
        ], "std-table")}
        <p class="card-actions">
          <button type="button" class="btn small" data-std-edit="${esc(s.id)}">Изменить</button>
          <a class="btn small" href="/api/standards/${encodeURIComponent(s.id)}" download="${esc(s.id)}.json">Скачать JSON</a>
          ${s.kind === "modified" ? `<button type="button" class="btn small" data-std-del="${esc(s.id)}" data-label="Вернуть исходный">Вернуть исходный</button>` : ""}
          ${s.kind === "custom" ? `<button type="button" class="btn small" data-std-del="${esc(s.id)}" data-label="Удалить">Удалить</button>` : ""}
        </p>
      </div>
    </details>`).join("");
    $("#std-list").querySelectorAll("[data-std-edit]").forEach((b) => {
      b.onclick = async () => {
        try { stdPage.open(await api.call("GET", `/api/standards/${encodeURIComponent(b.dataset.stdEdit)}`), false); } catch (e) { showError($("#std-editor"), e); }
      };
    });
    // удаление в два нажатия вместо модального окна
    $("#std-list").querySelectorAll("[data-std-del]").forEach((b) => {
      b.onclick = async () => {
        if (!b.classList.contains("confirm")) { b.classList.add("confirm"); b.textContent = "Точно? Нажмите ещё раз"; return; }
        try { await api.deleteStandard(b.dataset.stdDel); await stdPage.show(); } catch (e) { b.textContent = b.dataset.label; showError($("#std-editor"), e); }
      };
    });
  },
  open(data, isNew, note = "") {
    stdPage.editing = { data: JSON.parse(JSON.stringify(data)), isNew, note };
    stdPage.renderEditor();
    $("#std-editor").scrollIntoView({ behavior: "smooth", block: "start" });
  },
  blank: () => ({ id: "", title: "", cite: "", description: "", order: 100,
    criteria: [{ code: "", scope: "requirement", name: "", short: "", source: "", question: "" }] }),
  renderEditor() {
    const ed = stdPage.editing, el = $("#std-editor");
    if (!ed) { el.innerHTML = ""; return; }
    const d = ed.data;
    const f = (k, label, attrs = "") => `<label class="field">${label} <input data-f="${k}" value="${esc(d[k] ?? "")}" ${attrs}></label>`;
    el.innerHTML = `<div class="box std-editor">
      <h2>${ed.isNew ? "Новый стандарт" : `Изменить: ${esc(d.title)}`}</h2>
      ${ed.note ? `<p class="muted">${esc(ed.note)}</p>` : ""}
      ${!ed.isNew && stdPage.list.find((s) => s.id === d.id)?.kind !== "custom" ? `<p class="muted small">Встроенный файл не меняется: изменённая копия сохранится в custom_standards/, исходный можно вернуть одной кнопкой.</p>` : ""}
      <div class="grid2">
        ${f("id", "Идентификатор (латиница, цифры, _)", ed.isNew ? 'placeholder="my_standard"' : "readonly")}
        ${f("title", "Название", 'placeholder="Например: BABOK v3"')}
        ${f("cite", "Ссылка в отчёте", 'placeholder="полное название, год, разделы"')}
        ${f("order", "Порядок в списке", 'type="number" min="0" max="999"')}
      </div>
      <label class="field">Описание <input data-f="description" value="${esc(d.description || "")}"></label>
      <h3>Критерии</h3>
      <div id="std-crit">${d.criteria.map((c, i) => `<fieldset class="crit" data-i="${i}">
        <legend>Критерий ${i + 1}</legend>
        <div class="grid3">
          <label class="field">Код <input data-c="code" value="${esc(c.code)}" placeholder="${c.scope === "set" ? "set_coverage" : "my_rule"}"></label>
          <label class="field">Область <select data-c="scope">${Object.entries(SCOPE_LABEL).map(([k, l]) => `<option value="${k}" ${c.scope === k ? "selected" : ""}>${l}</option>`).join("")}</select></label>
          <label class="field">Короткое имя для таблицы <input data-c="short" value="${esc(c.short || "")}" maxlength="12"></label>
        </div>
        <div class="grid2">
          <label class="field">Название <input data-c="name" value="${esc(c.name || "")}"></label>
          <label class="field">Пункт стандарта <input data-c="source" value="${esc(c.source || "")}"></label>
        </div>
        <label class="field">Что проверяет агент <textarea data-c="question" rows="3">${esc(c.question || "")}</textarea></label>
        <p class="card-actions"><button type="button" class="link" data-crit-del="${i}">Убрать критерий</button></p>
      </fieldset>`).join("")}</div>
      <p><button type="button" class="btn small" id="crit-add">Добавить критерий</button></p>
      <div id="std-error"></div>
      <div class="row actions">
        <button type="button" class="btn primary" id="std-save">Сохранить</button>
        <button type="button" class="btn" id="std-cancel">Отмена</button>
      </div>
    </div>`;
    $("#crit-add").onclick = () => { stdPage.collect(); d.criteria.push({ code: "", scope: "requirement", name: "", short: "", source: "", question: "" }); stdPage.renderEditor(); };
    el.querySelectorAll("[data-crit-del]").forEach((b) => { b.onclick = () => { stdPage.collect(); d.criteria.splice(Number(b.dataset.critDel), 1); stdPage.renderEditor(); }; });
    $("#std-cancel").onclick = () => { stdPage.editing = null; stdPage.renderEditor(); };
    $("#std-save").onclick = stdPage.save;
  },
  collect() {
    const d = stdPage.editing.data;
    $("#std-editor").querySelectorAll("[data-f]").forEach((i) => { d[i.dataset.f] = i.dataset.f === "order" ? Number(i.value || 100) : i.value.trim(); });
    d.criteria = [...$("#std-editor").querySelectorAll("fieldset.crit")].map((fs) => {
      const c = {};
      fs.querySelectorAll("[data-c]").forEach((i) => { c[i.dataset.c] = i.value.trim(); });
      return c;
    });
    return d;
  },
  async save() {
    const ed = stdPage.editing, d = stdPage.collect();
    try {
      await api.saveStandard(d.id, d, ed.isNew);
      stdPage.editing = null;
      await stdPage.show();
      const card = document.querySelector(`.std-card[data-sid="${CSS.escape(d.id)}"]`);
      if (card) { card.open = true; card.scrollIntoView({ behavior: "smooth", block: "start" }); }
    } catch (e) { showError($("#std-error"), e); }
  },
  async importFile() {
    const file = $("#std-import").files[0];
    $("#std-import").value = "";
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      const exists = stdPage.list.some((s) => s.id === data.id);
      stdPage.open({ ...stdPage.blank(), ...data }, !exists, exists ? "Стандарт с таким идентификатором уже есть: при сохранении он будет заменён." : `Загружено из ${file.name}. Проверьте и сохраните.`);
    } catch (e) { stdPage.open(stdPage.blank(), true); showError($("#std-error"), new Error(`Не удалось прочитать ${file.name}: это не JSON стандарта`)); }
  },
  async aiDraft() {
    const text = $("#std-ai-text").value.trim();
    if (text.length < 3) { $("#std-ai-out").textContent = "Вставьте текст или название стандарта"; return; }
    $("#std-ai-run").disabled = true;
    $("#std-ai-out").textContent = "Модель составляет черновик…";
    try {
      const d = await api.draftStandard(text, settings.read());
      const model = d.model; delete d.model;
      $("#std-ai-out").textContent = "";
      stdPage.open(d, !stdPage.list.some((s) => s.id === d.id), `Черновик составила модель ${modelLabel(model)}. Проверьте пункты стандарта и формулировки перед сохранением.`);
    } catch (e) { $("#std-ai-out").textContent = e.message; }
    $("#std-ai-run").disabled = false;
  },
};

const plural = (n, one, few, many) => (n % 10 === 1 && n % 100 !== 11 ? one : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? few : many);

// ---------- страница «История» ----------

const runLabel = (r) => `${new Date(r.started).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })} / ${docName(r.document)}`;

async function showHistory() {
  const el = $("#history-table");
  el.textContent = "Загружаю…";
  try {
    const runs = await api.call("GET", "/api/runs?all=true");
    const when = (v) => (v ? new Date(v).toLocaleString("ru-RU") : "");
    el.innerHTML = table(runs, [
      ["started", "Проверка", (v, r) => (r.can_open ? `<a href="#/result/${esc(r.id)}">${esc(when(v))}</a>` : esc(when(v)))],
      ["document", "Документ", (v) => esc(docName(v))],
      ["status_label", "Статус", (v, r) => `<span class="st st-${esc(r.status)}">${esc(v)}</span>`
        + (r.error ? `<br><span class="muted small">${esc(r.error.slice(0, 160))}</span>` : "")],
      ["open_findings", "Нарушений", (v, r) => (r.can_open ? `${v} (критичных ${r.critical})` : "")],
      ["development", "", (v, r) => (r.can_retry
        ? `<button type="button" class="btn small" data-retry="${esc(r.id)}">Повторить</button>`
        : esc(v || ""))],
    ]);
    el.querySelectorAll("[data-retry]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        try {
          await api.call("POST", `/api/runs/${encodeURIComponent(b.dataset.retry)}/retry`);
          location.hash = "#/check";
          pollJobs();
        } catch (e) { showError(el, e); }
      };
    });
  } catch (e) { showError(el, e); }
}

// ---------- страница «До и после» ----------

async function showCompare() {
  try {
    const runs = await api.runs();
    const opts = runs.map((r) => `<option value="${esc(r.id)}">${esc(runLabel(r))}</option>`).join("");
    $("#cmp-before").innerHTML = opts; $("#cmp-after").innerHTML = opts;
    if (runs.length > 1) $("#cmp-before").selectedIndex = 1;
    // после «Проверить редакцию»: исходная проверка «до», проверка редакции «после»
    const pair = state.comparePair, j = pair && state.jobs.find((x) => x.id === pair.job);
    if (j?.run_id && runs.some((r) => r.id === pair.before)) { $("#cmp-before").value = pair.before; $("#cmp-after").value = j.run_id; }
  } catch (e) { showError($("#compare-out"), e); }
}

async function onCompare() {
  const a = $("#cmp-before").value, b = $("#cmp-after").value, out = $("#compare-out");
  if (!a || !b || a === b) { out.innerHTML = `<p class="error">Выберите две разные проверки</p>`; return; }
  try {
    const c = await api.compare(a, b);
    const rows = [["Документ", "document"], ["Требований", "requirements"], ["Открытых нарушений", "open"], ["Критично", "critical"],
      ["Существенно", "major"], ["Незначительно", "minor"], ["Вопросов", "questions"], ["Разработка", "development"]]
      .map(([label, k]) => ({ label, before: c.before[k], after: c.after[k] }));
    out.innerHTML = table(rows, [["label", "Показатель"], ["before", "До"], ["after", "После"]])
      + `<h3>Открытые нарушения по критериям</h3>` + table(c.by_criterion, [["name", "Критерий"], ["before", "До"], ["after", "После"]]);
  } catch (e) { showError(out, e); }
}

// ---------- маршрутизация и запуск ----------

function route() {
  const [, page = "check", arg] = location.hash.split("/");
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== `view-${page}`; });
  document.querySelectorAll("[data-route]").forEach((a) => a.classList.toggle("active", a.dataset.route === page));
  if (page === "result") showResult(arg && decodeURIComponent(arg)); else assistant.close();
  if (page === "history") showHistory();
  if (page === "compare") showCompare();
  if (page === "standards") stdPage.show();
}

function applyTheme(dark) {
  if (dark) document.documentElement.dataset.theme = "dark";
  else delete document.documentElement.dataset.theme;
  const b = $("#theme-toggle");
  b.textContent = dark ? "Светлая тема" : "Тёмная тема";
  b.setAttribute("aria-pressed", String(dark));
  try { localStorage.setItem("validator.theme", dark ? "dark" : "light"); } catch (e) { /* без сохранения */ }
}

async function init() {
  applyTheme(document.documentElement.dataset.theme === "dark");
  $("#theme-toggle").onclick = () => applyTheme(document.documentElement.dataset.theme !== "dark");
  $("#settings-toggle").onclick = () => { $("#settings").hidden = !$("#settings").hidden; };
  $("#settings-close").onclick = () => { $("#settings").hidden = true; };
  $("#opt-provider").onchange = () => { settings.fillProvider(settings.saved().perProvider?.[settings.provider()]); settings.read(); };
  $("#opt-key").oninput = (e) => { state.keys[settings.provider()] = e.target.value.trim(); settings.showKey(); };
  $("#opt-model").oninput = () => settings.showKey();
  $("#settings").addEventListener("change", (e) => { if (e.target.id !== "opt-provider") settings.read(); });
  $("#file-input").onchange = () => { addFiles([...$("#file-input").files]); $("#file-input").value = ""; };
  setupDrop();
  $("#draft-edit").onclick = draft.edit;
  $("#draft-save").onclick = draft.save;
  $("#draft-cancel").onclick = () => { $("#draft-editor").hidden = true; };
  $("#draft-undo").onclick = () => draft.run(() => api.draftUndo(state.run.id), "Последняя правка отменена");
  $("#draft-reset").onclick = () => draft.run(() => api.draftReset(state.run.id), "Возвращён исходный текст (это можно отменить)");
  $("#draft-check").onclick = draft.check;
  $("#std-new").onclick = () => stdPage.open(stdPage.blank(), true);
  $("#std-import").onchange = stdPage.importFile;
  $("#std-ai-toggle").onclick = () => { $("#std-ai").hidden = !$("#std-ai").hidden; if (!$("#std-ai").hidden) $("#std-ai-text").focus(); };
  $("#std-ai-run").onclick = stdPage.aiDraft;
  $("#std-manage").onclick = () => { $("#settings").hidden = true; };
  const navH = () => document.documentElement.style.setProperty("--nav-h", `${$(".navbar").offsetHeight}px`);
  navH(); window.addEventListener("resize", navH);
  $("#btn-run").onclick = onRun;
  $("#btn-compare").onclick = onCompare;
  $("#f-text").oninput = (e) => { state.filter.text = e.target.value; state.filter.shown = 8; if (state.run) renderFindings(state.run); };
  $("#assistant-open").onclick = () => assistant.open();
  $("#assistant-close").onclick = () => assistant.close();
  $("#ask").onsubmit = (e) => { e.preventDefault(); const v = $("#ask-input").value; $("#ask-input").value = ""; assistant.ask(v); };
  $("#ask-input").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#ask").requestSubmit(); } };
  document.querySelector('details[data-more="report"]').addEventListener("toggle", (e) => {
    if (e.target.open && state.run && !$("#report-frame").dataset.run?.startsWith(state.run.id)) {
      $("#report-frame").dataset.run = state.run.id;
      api.reportHtml(state.run.id).then((html) => { $("#report-frame").srcdoc = docPage(html); });
    }
  });
  window.addEventListener("hashchange", route);

  state.status = await api.status();
  settings.load();
  if (!settings.providerInfo(settings.provider()).has_env_key) $("#settings").hidden = false;
  renderDocs();
  pollJobs();  // после перезагрузки страницы продолжаем следить за идущими проверками
  route();
}

init();
