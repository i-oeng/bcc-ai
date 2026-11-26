// Каркас фронтенда без сборки и зависимостей. Разделы:
//   api      запросы к /api (единственное место, где есть fetch)
//   settings настройки прогона (localStorage) и ключ (только память)
//   views    по функции на страницу; маршрут в адресе: #/check, #/result/<id>, #/history, #/compare
// Оформление временное: разметка в index.html, стили в style.css.
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const SEV = { critical: "Критично", major: "Существенно", minor: "Незначительно" };
const STATUS = { open: "открыто", closed: "закрыто ответом", changed: "изменено ответом" };

// ---------- api ----------

const api = {
  async call(method, path, body, isForm) {
    const headers = {};
    if (state.apiKey) headers["X-Gemini-Key"] = state.apiKey;
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
  job: (id, from) => api.call("GET", `/api/jobs/${id}?log_from=${from}`),
  runs: () => api.call("GET", "/api/runs"),
  run: (id) => api.call("GET", `/api/runs/${encodeURIComponent(id)}`),
  reportHtml: (id) => api.call("GET", `/api/runs/${encodeURIComponent(id)}/report`),
  fileUrl: (id, kind) => `/api/runs/${encodeURIComponent(id)}/files/${kind}`,
  answer: (id, answers, options) => api.call("POST", `/api/runs/${encodeURIComponent(id)}/answers`, { answers, options }),
  compare: (a, b) => api.call("GET", `/api/compare?before=${encodeURIComponent(a)}&after=${encodeURIComponent(b)}`),
};

// ---------- состояние и настройки ----------

const state = {
  apiKey: "",          // только в памяти вкладки
  documentId: null,    // sample:<имя> | upload:<имя>
  jobId: null,
  logFrom: 0,
  run: null,           // JSON /api/runs/<id>
};

const settings = {
  load(defaults) {
    const saved = JSON.parse(localStorage.getItem("validator.options") || "{}");
    const o = { ...defaults, ...saved };
    $("#opt-model").value = o.model;
    $("#opt-fallbacks").value = (o.fallbacks || []).join(", ");
    $("#opt-batch").value = o.batch_size;
    $("#opt-redact").checked = o.redact;
    $("#opt-ner").checked = o.redact_ner;
    $("#opt-cache").checked = o.use_cache;
    $("#opt-fix").checked = !!o.fix;
  },
  read() {
    const o = {
      model: $("#opt-model").value.trim() || null,
      fallbacks: $("#opt-fallbacks").value.split(",").map((s) => s.trim()).filter(Boolean),
      batch_size: Number($("#opt-batch").value) || null,
      redact: $("#opt-redact").checked,
      redact_ner: $("#opt-ner").checked,
      use_cache: $("#opt-cache").checked,
      fix: $("#opt-fix").checked,
    };
    localStorage.setItem("validator.options", JSON.stringify(o));
    return o;
  },
};

// ---------- общие куски разметки ----------

function table(rows, cols) {
  if (!rows.length) return `<p class="muted">Нет данных</p>`;
  const head = cols.map(([, title]) => `<th>${esc(title)}</th>`).join("");
  const body = rows.map((r) => `<tr>${cols.map(([key, , fmt]) => `<td>${fmt ? fmt(r[key], r) : esc(r[key])}</td>`).join("")}</tr>`).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

const cards = (items) => `<div class="cards">${items.map(([label, value]) => `<div class="card"><b>${esc(value)}</b>${esc(label)}</div>`).join("")}</div>`;
// HTML документа или отчёта показываем только в iframe с sandbox (без скриптов), со своими стилями
const docPage = (html) => `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="${location.origin}/static/doc.css"><body>${html}</body>`;
const showError = (el, e) => { el.innerHTML = `<p class="error">${esc(e.message || e)}</p>`; };

// ---------- страница «Проверка» ----------

async function selectDocument(info) {
  state.documentId = info.id;
  $("#doc-info").innerHTML = cards([["формат", info.format.toUpperCase()], ["строк", info.lines], ["символов", info.chars]])
    + `<p class="muted">«${esc(info.title)}» · SHA-256 ${esc(info.sha256.slice(0, 16))}…</p>`;
  $("#btn-preview").disabled = false;
  $("#btn-run").disabled = !!state.jobId;
  $("#preview").innerHTML = "";
}

async function onSample() {
  const id = $("#sample-select").value;
  if (!id) return;
  $("#file-input").value = "";
  $("#doc-info").textContent = "Читаю документ…";
  try { await selectDocument(await api.document(id)); } catch (e) { showError($("#doc-info"), e); }
}

async function onFile() {
  const f = $("#file-input").files[0];
  if (!f) return;
  $("#sample-select").value = "";
  $("#doc-info").textContent = "Загружаю…";
  try { await selectDocument(await api.upload(f)); } catch (e) { showError($("#doc-info"), e); }
}

async function onPreview() {
  const out = $("#preview");
  out.textContent = "Маскирую локально, в модель ничего не уходит…";
  try {
    const p = await api.preview(state.documentId, settings.read());
    out.innerHTML = (p.replacements.length
      ? `<p class="ok">Замаскировано значений: ${p.replacements.length}</p>` + table(p.replacements, [["placeholder", "Метка"], ["kind", "Тип"], ["value", "Исходное (частично)"]])
      : `<p>Чувствительных данных не найдено${p.enabled ? "" : " (маскирование выключено)"}</p>`)
      + `<h3>Документ, который увидит модель</h3>
         <p class="muted">Подсвечены замаскированные значения. <label><input type="checkbox" id="raw-toggle"> исходный текст</label></p>
         <iframe id="masked-frame" class="docframe" sandbox title="Маскированный документ"></iframe>
         <pre class="masked" id="masked-raw" hidden>${esc(p.masked)}</pre>`;
    $("#masked-frame").srcdoc = docPage(p.masked_html);
    $("#raw-toggle").onchange = (e) => { $("#masked-frame").hidden = e.target.checked; $("#masked-raw").hidden = !e.target.checked; };
  } catch (e) { showError(out, e); }
}

async function onRun() {
  try {
    const j = await api.startJob(state.documentId, settings.read());
    trackJob(j.id);
  } catch (e) { showError($("#job"), e); }
}

function trackJob(id) {
  state.jobId = id;
  state.logFrom = 0;
  $("#btn-run").disabled = true;
  $("#job").innerHTML = `<p id="job-status"></p><pre class="log" id="job-log"></pre>`;
  pollJob();
}

async function pollJob() {
  if (!state.jobId) return;
  let j;
  try { j = await api.job(state.jobId, state.logFrom); } catch (e) { showError($("#job"), e); state.jobId = null; return; }
  state.logFrom = j.log_next;
  const log = $("#job-log");
  if (log && j.log.length) { log.textContent += j.log.join("\n") + "\n"; log.scrollTop = log.scrollHeight; }
  const st = $("#job-status");
  $("#job-indicator").textContent = j.status === "running" ? `идёт проверка ${j.document}, ${Math.round(j.elapsed)} с` : "";
  if (j.status === "running") {
    if (st) st.textContent = `Проверка «${j.document}» идёт ${Math.round(j.elapsed)} с`;
    setTimeout(pollJob, 1500);
    return;
  }
  state.jobId = null;
  $("#btn-run").disabled = !state.documentId;
  if (j.status === "done") {
    if (st) st.innerHTML = `<span class="ok">Готово за ${Math.round(j.elapsed)} с.</span> <a href="#/result/${esc(j.run_id)}">Открыть результат</a>`;
    location.hash = `#/result/${j.run_id}`;
  } else if (st) {
    st.innerHTML = `<span class="error">Прогон остановился: ${esc(j.error)}</span> Готовые шаги в кэше, повторный запуск продолжит с места остановки.`;
  }
}

// ---------- страница «Результат» ----------

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
  $("#res-meta").textContent = `${r.document.name} · ${new Date(r.started).toLocaleString("ru-RU")} · прогон ${r.id}`;
  renderOverview(r); renderFindings(r); renderRequirements(r); renderQuestions(r); renderChecklist(r); renderPassport(r); renderFiles(r);
  api.reportHtml(r.id).then((html) => {
    $("#report-frame").srcdoc = docPage(html);
  });
}

function renderOverview(r) {
  const s = r.summary, d = r.decision;
  $("#tab-overview").innerHTML = cards([["требований", s.requirements], ["открытых нарушений", s.open], ["критичных", s.critical],
    ["существенных", s.major], ["незначительных", s.minor], ["вопросов", s.questions], ...(s.closed ? [["закрыто ответами", s.closed]] : [])])
    + `<p><b>Разработка:</b> ${esc(d.development)}<br><span class="muted">${esc(d.development_rationale)}</span></p>`
    + `<p><b>Тестирование:</b> ${esc(d.testing)}<br><span class="muted">${esc(d.testing_rationale)}</span></p>`
    + `<p>${esc(d.overall)}</p>`
    + (d.fix_first.length ? `<h3>Исправить в первую очередь</h3><ol>${d.fix_first.map((x) => `<li><b>${esc(x.title)}</b> (${esc(x.finding_ids.join(", "))}): ${esc(x.action)}</li>`).join("")}</ol>` : "");
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
    + `<label><input id="ans-fix" type="checkbox" checked> Сформировать исправленную редакцию</label>
       <div class="row"><button id="btn-answers" class="primary" type="button">Перепроверить с ответами</button></div><div id="answers-out"></div>`;
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
    const j = await api.answer(state.run.id, answers, { ...settings.read(), fix: $("#ans-fix").checked });
    location.hash = "#/check";
    trackJob(j.id);
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
      <li>Версия агента: ${esc(r.agent_version)}; модель: ${esc(p.model)}; запросов: ${p.calls}, из кэша: ${p.cached}</li>
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

async function showHistory() {
  const el = $("#history-table");
  el.textContent = "Загружаю…";
  try {
    const runs = await api.runs();
    el.innerHTML = table(runs, [
      ["id", "Прогон", (v) => `<a href="#/result/${esc(v)}">${esc(v)}</a>`], ["document", "Документ"], ["requirements", "Требований"],
      ["open_findings", "Открытых нарушений"], ["critical", "Критичных"], ["answered", "С ответами", (v) => (v ? "да" : "")], ["development", "Разработка"],
    ]);
  } catch (e) { showError(el, e); }
}

// ---------- страница «До и после» ----------

async function showCompare() {
  try {
    const runs = await api.runs();
    const opts = runs.map((r) => `<option value="${esc(r.id)}">${esc(r.id)} · ${esc(r.document)}</option>`).join("");
    $("#cmp-before").innerHTML = opts; $("#cmp-after").innerHTML = opts;
    if (runs.length > 1) $("#cmp-before").selectedIndex = 1;
  } catch (e) { showError($("#compare-out"), e); }
}

async function onCompare() {
  const a = $("#cmp-before").value, b = $("#cmp-after").value, out = $("#compare-out");
  if (!a || !b || a === b) { out.innerHTML = `<p class="error">Выберите два разных прогона</p>`; return; }
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
  $("#opt-key").oninput = (e) => { state.apiKey = e.target.value.trim(); };
  $("#sample-select").onchange = onSample;
  $("#file-input").onchange = onFile;
  $("#btn-preview").onclick = onPreview;
  $("#btn-run").onclick = onRun;
  $("#btn-compare").onclick = onCompare;
  document.querySelectorAll("#res-tabs button").forEach((b) => { b.onclick = () => switchTab(b.dataset.tab); });
  ["#f-sev", "#f-status", "#f-text"].forEach((s) => { $(s).oninput = () => state.run && renderFindings(state.run); });
  window.addEventListener("hashchange", route);

  const st = await api.status();
  settings.load(st.defaults);
  $("#key-status").textContent = st.has_env_key ? "Ключ найден в .env" : "В .env нет GEMINI_API_KEY: впишите ключ ниже";
  if (!st.has_env_key) $("#settings").hidden = false;
  $("#sample-select").innerHTML += (await api.samples()).map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
  if (st.running_job) trackJob(st.running_job);  // после перезагрузки страницы продолжаем следить за прогоном
  route();
}

init();
