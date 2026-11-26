// Фронтенд без сборки и зависимостей. Разделы:
//   api       запросы к /api (единственное место, где есть fetch)
//   settings  настройки проверки (localStorage) и ключи провайдеров (только память вкладки)
//   документы, проверки (очередь, остановка), результат, история, сравнение
// Маршрут в адресе: #/check, #/result/<id>, #/history, #/compare.
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (n) => Number(n || 0).toLocaleString("ru-RU");
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
  compare: (a, b) => api.call("GET", `/api/compare?before=${encodeURIComponent(a)}&after=${encodeURIComponent(b)}`),
};

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
    const chosen = saved.standards || d.standards;
    $("#opt-standards").innerHTML = st.standards.map((s) => `
      <label class="check std" title="${esc(s.description)}">
        <input type="checkbox" value="${esc(s.id)}" ${s.mandatory ? "checked disabled" : chosen.includes(s.id) ? "checked" : ""}>
        <span><b>${esc(s.title)}</b>${s.mandatory ? " (всегда)" : ""}<br><span class="muted small">${esc(s.description)}</span></span>
      </label>`).join("");
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
    $("#status-key").textContent = own ? "Ключ: из настроек" : p.has_env_key ? "Ключ: .env" : "Ключ: не задан";
    $("#status-model").textContent = `${p.title}: ${$("#opt-model").value.trim() || p.model}`;
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
  if (!state.docs.length) { el.innerHTML = `<p class="muted">Добавьте один или несколько документов: PDF, DOCX, MD, TXT, HTML.</p>`; return; }
  el.innerHTML = table(state.docs, [
    ["name", "Документ", (v, d) => `<b>${esc(v)}</b><br><span class="muted">«${esc(d.title)}»</span>`],
    ["format", "Формат", (v) => esc(v.toUpperCase())],
    ["lines", "Строк", (v) => num(v)],
    ["chars", "Символов", (v) => num(v)],
    ["id", "", (v) => `<button type="button" class="link" data-preview="${esc(v)}">Что уйдёт в модель</button> · <button type="button" class="link" data-remove="${esc(v)}">Убрать</button>`],
  ], "doclist");
  el.querySelectorAll("[data-preview]").forEach((b) => { b.onclick = () => onPreview(b.dataset.preview); });
  el.querySelectorAll("[data-remove]").forEach((b) => { b.onclick = () => { state.docs = state.docs.filter((d) => d.id !== b.dataset.remove); renderDocs(); }; });
}

async function onSample() {
  const id = $("#sample-select").value;
  $("#sample-select").value = "";
  if (!id) return;
  try { addDoc(await api.document(id)); } catch (e) { showError($("#preview"), e); }
}

async function onFiles() {
  const files = [...$("#file-input").files];
  $("#file-input").value = "";
  for (const f of files) {
    try { addDoc(await api.upload(f)); } catch (e) { $("#preview").innerHTML = win("Ошибка загрузки", `<p class="error">${esc(f.name)}: ${esc(e.message)}</p>`); }
  }
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

function renderJobs() {
  const active = state.jobs.filter((j) => j.status === "running").length;
  const queued = state.jobs.filter((j) => j.status === "queued").length;
  const ind = $("#job-indicator");
  ind.textContent = active || queued ? `Идут проверки: ${active}` + (queued ? `, в очереди: ${queued}` : "") : "Готово";
  ind.classList.toggle("busy", !!(active || queued));
  $("#jobs-box").hidden = !state.jobs.length;
  if (!state.jobs.length) return;
  $("#jobs").innerHTML = state.jobs.map((j) => {
    const live = j.status === "queued" || j.status === "running";
    const bar = { running: "progress", done: "progress done", queued: "progress queued" }[j.status] || "progress failed";
    const log = state.logs[j.id];
    return `<div class="job ${esc(j.status)}">
      <div class="job-head">
        <b>${esc(j.document)}</b>
        <span class="job-status">${esc(JOB_STATUS[j.status] || j.status)}${j.stop_requested && live ? " (останавливается)" : ""}${j.elapsed ? ` · ${fmtTime(j.elapsed)}` : ""}</span>
        <span class="job-actions">
          ${live ? `<button type="button" class="btn small" data-stop="${esc(j.id)}" ${j.stop_requested ? "disabled" : ""}>Остановить</button>` : ""}
          ${j.status === "done" ? `<a class="btn small primary" href="#/result/${esc(j.run_id)}">Открыть результат</a>` : ""}
          <button type="button" class="link" data-log="${esc(j.id)}">${state.openLogs.has(j.id) ? "Скрыть журнал" : "Журнал"}</button>
        </span>
      </div>
      <div class="${bar}"><span></span></div>
      <p class="muted small job-last">${esc(j.error || j.last)}</p>
      ${state.openLogs.has(j.id) ? `<pre class="log">${esc(log ? log.text : "…")}</pre>` : ""}
    </div>`;
  }).join("");
  document.querySelectorAll("[data-stop]").forEach((b) => { b.onclick = () => onStop(b.dataset.stop); });
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

// HTML документа или отчёта показываем только в iframe с sandbox (без скриптов), со своими стилями
const docPage = (html) => `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="${location.origin}/static/doc.css"><body>${html}</body>`;

async function showResult(runId) {
  if (!runId) runId = state.run?.id;
  $("#res-empty").hidden = !!runId;
  $("#res-body").hidden = !runId;
  if (!runId) return;
  if (state.run?.id !== runId) {
    $("#res-title").textContent = "Загружаю…";
    try { state.run = await api.run(runId); } catch (e) { showError($("#res-meta"), e); return; }
  }
  const r = state.run;
  $("#res-title").textContent = r.document.title || r.document.name;
  $("#res-meta").textContent = `${r.document.name} · проверка от ${new Date(r.started).toLocaleString("ru-RU")}`
    + (r.elapsed ? ` · ${fmtTime(r.elapsed)}` : "");
  renderOverview(r); renderFindings(r); renderRequirements(r); renderQuestions(r); renderChecklist(r); renderPassport(r); renderFiles(r);
  api.reportHtml(r.id).then((html) => { $("#report-frame").srcdoc = docPage(html); });
}

function renderOverview(r) {
  const s = r.summary, d = r.decision;
  $("#tab-overview").innerHTML = figures([["требований", s.requirements], ["открытых нарушений", s.open], ["критичных", s.critical, "critical"],
    ["существенных", s.major, "major"], ["незначительных", s.minor, "minor"], ["вопросов", s.questions], ...(s.closed ? [["закрыто ответами", s.closed]] : [])])
    + `<p><b>Разработка:</b> ${esc(d.development)}<br><span class="muted">${esc(d.development_rationale)}</span></p>`
    + `<p><b>Тестирование:</b> ${esc(d.testing)}<br><span class="muted">${esc(d.testing_rationale)}</span></p>`
    + `<p>${esc(d.overall)}</p>`
    + (d.fix_first.length ? `<h3>Исправить в первую очередь</h3><ol>${d.fix_first.map((x) => `<li><b>${esc(x.title)}</b> (${esc(x.finding_ids.join(", "))}): ${esc(x.action)}</li>`).join("")}</ol>` : "")
    + `<p class="muted small">Стандарты: ${esc(r.standards.map((x) => x.title).join(", "))}</p>`;
}

function renderFindings(r) {
  const sev = $("#f-sev").value, status = $("#f-status").value, q = $("#f-text").value.toLowerCase();
  const rows = r.findings.filter((f) => (!sev || f.severity === sev) && (!status || f.status === status)
    && (!q || `${f.id} ${f.problem} ${f.fix} ${f.criterion_name}`.toLowerCase().includes(q)));
  $("#findings-table").innerHTML = `<p class="muted">Показано: ${rows.length} из ${r.findings.length}</p>` + table(rows, [
    ["id", "ID"],
    ["severity", "Серьёзность", (v) => `<span class="sev-${esc(v)}">${esc(SEV[v] || v)}</span>`],
    ["criterion_name", "Критерий"],
    ["req_ids", "Требования", (v) => esc(v.join(", "))],
    ["anchors", "Где и цитата", (v) => v.slice(0, 2).map((a) => `${esc(a.where)}<br><i>«${esc(a.text.slice(0, 200))}»</i>`).join("<hr>")],
    ["problem", "Проблема"],
    ["fix", "Исправление"],
    ["status", "Статус", (v, f) => esc(STATUS[v] || v) + (f.resolution_note ? `<br><span class="muted">${esc(f.resolution_note)}</span>` : "")],
  ]);
}

function renderRequirements(r) {
  $("#tab-requirements").innerHTML = table(r.requirements, [
    ["id", "ID"], ["doc_id", "В документе"], ["kind", "Тип"], ["summary", "Суть"], ["where", "Где"], ["open_findings", "Нарушений"],
  ]);
}

function renderQuestions(r) {
  const el = $("#tab-questions");
  if (!r.questions.length) { el.innerHTML = `<p class="muted">Вопросов нет</p>`; return; }
  const prev = Object.fromEntries(r.answers.map((a) => [a.id, a.answer]));
  el.innerHTML = `<p class="muted">Впишите ответы заказчика и запустите повторную проверку: агент закроет дефекты, которые ответы устраняют, и пересчитает готовность.</p>`
    + r.questions.map((q) => {
      const opts = q.options.map((o, i) => `Вариант ${i + 1}: ${o}`);
      const own = prev[q.id] && !opts.includes(prev[q.id]) ? prev[q.id] : "";
      return `<div class="question" data-qid="${esc(q.id)}">
        <b>${esc(q.id)}. ${esc(q.question)}</b>
        <div class="muted">${esc(q.priority_label)} · кому: ${esc(q.addressee)}</div>
        ${q.quote ? `<div class="muted"><i>«${esc(q.quote.slice(0, 300))}»</i></div>` : ""}
        <div class="muted">Почему важно: ${esc(q.why)}</div>
        ${opts.map((o) => `<label><input type="radio" name="q-${esc(q.id)}" value="${esc(o)}" ${prev[q.id] === o ? "checked" : ""}> ${esc(o)}</label>`).join("")}
        <label><input type="radio" name="q-${esc(q.id)}" value="__own" ${own ? "checked" : ""}> Свой ответ: <input class="own" value="${esc(own)}"></label>
      </div>`;
    }).join("")
    + `<label class="check"><input id="ans-fix" type="checkbox" checked> Сформировать исправленную редакцию</label>
       <div class="row actions"><button id="btn-answers" class="btn primary" type="button">Перепроверить с ответами</button></div><div id="answers-out"></div>`;
  $("#btn-answers").onclick = onAnswers;
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

function renderChecklist(r) {
  $("#tab-checklist").innerHTML =
    `<h3>Чек-лист ревью требований</h3>` + table(r.checklist, [["item", "Пункт"], ["answer", "Ответ"], ["rationale", "Обоснование"], ["finding_ids", "Находки", (v) => esc(v.join(", "))]])
    + `<h3>Покрытие функциональных требований критериями приёмки</h3>` + table(r.coverage, [["req_id", "Требование"], ["acceptance_ids", "Критерии приёмки", (v) => esc(v.join(", ") || "нет")], ["missing_scenarios", "Непокрытые сценарии", (v) => esc(v.join("; "))]])
    + `<h3>Критерии набора</h3>` + table(r.set_verdicts, [["criterion_name", "Критерий"], ["verdict", "Оценка"], ["comment", "Комментарий"]]);
}

function renderPassport(r) {
  const p = r.passport, m = Object.entries(p.masking);
  $("#tab-passport").innerHTML = `<ul>
      <li>Документ: ${esc(r.document.name)}, SHA-256 ${esc(r.document.sha256.slice(0, 16))}…</li>
      <li>Стандарты: ${esc(r.standards.map((x) => x.title).join(", "))}</li>
      <li>Версия агента: ${esc(r.agent_version)}; провайдер: ${esc(p.provider)}; модель: ${esc(p.model)}; запросов: ${p.calls}, из кэша: ${p.cached}${r.elapsed ? `; время: ${fmtTime(r.elapsed)}` : ""}</li>
      <li>Маскирование: ${m.length ? esc(m.map(([k, v]) => `${k}: ${v}`).join(", ")) : "чувствительных данных не найдено"}</li>
    </ul>` + table(p.steps, [["step", "Шаг"], ["model", "Модель"]])
    + p.warnings.map((w) => `<p class="error">${esc(w)}</p>`).join("");
}

function renderFiles(r) {
  const files = [["report", "Отчёт (Markdown)"], ["questions", "Вопросы заказчику"], ...(r.has_fixed ? [["fixed", "Исправленная редакция"]] : []), ["result", "result.json"]];
  $("#tab-files").innerHTML = `<ul>${files.map(([k, label]) => `<li><a href="${api.fileUrl(r.id, k)}">${esc(label)}</a></li>`).join("")}</ul>`;
}

function switchTab(name) {
  document.querySelectorAll("#res-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll("#res-body .tab").forEach((t) => { t.hidden = t.id !== `tab-${name}`; });
}

// ---------- страница «История» ----------

const runLabel = (r) => `${new Date(r.started).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })} · ${r.document}`;

async function showHistory() {
  const el = $("#history-table");
  el.textContent = "Загружаю…";
  try {
    const runs = await api.runs();
    el.innerHTML = table(runs, [
      ["started", "Проверка", (v, r) => `<a href="#/result/${esc(r.id)}">${esc(new Date(v).toLocaleString("ru-RU"))}</a>`],
      ["document", "Документ"], ["requirements", "Требований"], ["open_findings", "Открытых нарушений"], ["critical", "Критичных"],
      ["answered", "С ответами", (v) => (v ? "да" : "")], ["development", "Разработка"],
    ]);
  } catch (e) { showError(el, e); }
}

// ---------- страница «До и после» ----------

async function showCompare() {
  try {
    const runs = await api.runs();
    const opts = runs.map((r) => `<option value="${esc(r.id)}">${esc(runLabel(r))}</option>`).join("");
    $("#cmp-before").innerHTML = opts; $("#cmp-after").innerHTML = opts;
    if (runs.length > 1) $("#cmp-before").selectedIndex = 1;
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
  if (page === "result") showResult(arg && decodeURIComponent(arg));
  if (page === "history") showHistory();
  if (page === "compare") showCompare();
}

async function init() {
  $("#settings-toggle").onclick = () => { $("#settings").hidden = !$("#settings").hidden; };
  $("#settings-close").onclick = () => { $("#settings").hidden = true; };
  $("#opt-provider").onchange = () => { settings.fillProvider(settings.saved().perProvider?.[settings.provider()]); settings.read(); };
  $("#opt-key").oninput = (e) => { state.keys[settings.provider()] = e.target.value.trim(); settings.showKey(); };
  $("#opt-model").oninput = () => settings.showKey();
  $("#settings").addEventListener("change", (e) => { if (e.target.id !== "opt-provider") settings.read(); });
  $("#sample-select").onchange = onSample;
  $("#file-input").onchange = onFiles;
  $("#btn-run").onclick = onRun;
  $("#btn-compare").onclick = onCompare;
  document.querySelectorAll("#res-tabs button").forEach((b) => { b.onclick = () => switchTab(b.dataset.tab); });
  ["#f-sev", "#f-status", "#f-text"].forEach((s) => { $(s).oninput = () => state.run && renderFindings(state.run); });
  window.addEventListener("hashchange", route);

  state.status = await api.status();
  settings.load();
  if (!settings.providerInfo(settings.provider()).has_env_key) $("#settings").hidden = false;
  $("#status-version").textContent = `Версия ${state.status.version}. Одновременно идут до ${state.status.max_parallel} проверок.`;
  $("#footer-standards").textContent = "Стандарты: " + state.status.standards.map((s) => s.title).join(", ") + ".";
  $("#sample-select").innerHTML += (await api.samples()).map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
  renderDocs();
  pollJobs();  // после перезагрузки страницы продолжаем следить за идущими проверками
  route();
}

init();
