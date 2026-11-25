"""Отчёт о проверке в формате Markdown (на русском)."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .criteria import ALL, REQUIREMENT_CRITERIA, SET_CRITERIA, SEVERITIES, SEVERITY_RULES
from .grounding import Anchor
from .pipeline import RunResult
from .schemas import Finding

SHORT = {
    "necessary": "Необх.",
    "appropriate": "Уместн.",
    "unambiguous": "Однозн.",
    "complete": "Полн.",
    "singular": "Един.",
    "feasible": "Выполн.",
    "verifiable": "Провер.",
    "correct": "Корр.",
    "conforming": "Оформл.",
}
MARK = {"ok": "✅", "violation": "❌", "na": "·"}
SET_MARK = {"ok": "✅ соответствует", "partial": "⚠️ частично", "violation": "❌ нарушено"}
SEV_ICON = {"critical": "🔴", "major": "🟠", "minor": "🟡"}
KIND = {
    "functional": "функциональное",
    "nonfunctional": "нефункциональное",
    "data": "данные",
    "integration": "интеграция",
    "interface": "интерфейс",
    "api": "API",
    "acceptance": "критерий приёмки",
    "business": "бизнес-цель",
    "constraint": "ограничение",
}
PRIORITY = {"blocking": "Блокирующие", "important": "Важные", "nice": "Желательные"}


class Renderer:
    def __init__(self, res: RunResult):
        self.res = res
        self.R = res.redactor.restore if res.redactor else (lambda s: s)
        self.by_id = {f.fid: f for f in res.findings}

    # ---- форматирование ----

    def t(self, s: str, model: bool = True) -> str:
        """Вернуть замаскированные значения на место.

        model=True: текст, написанный моделью; длинные тире заменяются двоеточием
        (стиль отчёта). Цитаты документа (model=False) не меняются ни на символ.
        """
        s = self.R(s or "").strip()
        if model:
            s = re.sub(r"\s+[—–]\s+", ": ", s)
        return s

    def cell(self, s: str, limit: int | None = None, model: bool = True) -> str:
        s = self.t(s, model).replace("\n", " ")
        s = re.sub(r"\*\*|`", "", s)
        s = s.replace("|", "\\|")
        if limit and len(s) > limit:
            s = s[: limit - 1].rstrip() + "…"
        return s

    def quote(self, a: Anchor, limit: int = 220) -> str:
        q = self.cell(a.text, limit, model=False)
        return f"«{q}» ({a.where()})"

    def fid_link(self, fid: str) -> str:
        return f"[{fid}](#{fid.lower()})"

    # ---- разделы ----

    def render(self) -> str:
        parts = [
            self.header(),
            self.passport(),
            self.segmentation(),
            self.per_requirement(),
            self.set_level(),
            self.questions(),
            self.summary(),
            self.verdict(),
            self.appendix(),
        ]
        return "\n\n".join(p.strip("\n") for p in parts if p) + "\n"

    def header(self) -> str:
        r = self.res
        return (
            f"# Отчёт о проверке требований\n\n"
            f"**Документ:** {self.cell(r.doc.title, model=False)}  \n"
            f"**Итог:** разработка: {r.decision_dev.lower()}; тестирование: {r.decision_test.lower()}.\n\n"
            "Отчёт сформирован агентом автоматически. Каждое нарушение ссылается на строку проверяемого документа."
        )

    def passport(self) -> str:
        r = self.res
        u = r.usage
        model = f"Google Gemini `{r.config.model}`"
        if u and u.steps:
            used = Counter(m for _, m in u.steps)
            if set(used) != {r.config.model}:
                model += "; фактически ответили: " + ", ".join(f"`{m}` (шагов: {n})" for m, n in used.most_common())
                model += " (основная модель была перегружена, сработала запасная цепочка)"
        red = r.redactor.stats() if r.redactor else {}
        if not r.config.redact:
            red_txt = "отключено"
        elif red:
            red_txt = "включено; заменено: " + ", ".join(f"{k} {v}" for k, v in red.items())
        else:
            red_txt = "включено; чувствительных данных в документе не найдено"
        dur = ""
        if r.finished:
            secs = int((r.finished - r.started).total_seconds())
            dur = f"{secs // 60} мин {secs % 60} с"
        rows = [
            ("Название документа", self.cell(r.doc.title, model=False)),
            ("Файл", f"`{r.doc.path.name}` ({r.doc.fmt.upper()}, {len(r.doc.lines)} строк)"),
            ("SHA-256 файла", f"`{r.doc.sha256[:16]}…`"),
            ("Дата прогона", r.started.strftime("%d.%m.%Y %H:%M") + f" ({r.config.tz.key})"),
            ("Модель", model),
            ("Версия агента", r.agent_version),
            ("Критерии", "ISO/IEC/IEEE 29148:2018 п. 5.2.5, 5.2.6; IEEE Std 830-1998 п. 4.3"),
            ("Маскирование данных", red_txt),
        ]
        if u:
            rows.append(("Запросов к модели", f"{u.calls}; токенов: вход {u.prompt_tokens}, выход {u.output_tokens}"
                         + (f", рассуждение {u.thinking_tokens}" if u.thinking_tokens else "")))
        if u and getattr(u, "cached", 0):
            rows.append(("Кэш ответов", f"шагов взято из кэша: {u.cached} (прогон продолжен после сбоя или повторён)"))
        if dur:
            rows.append(("Длительность", dur))
        for w in getattr(r, "warnings", []) or []:
            rows.append(("Предупреждение", self.cell(w, 300)))
        if r.audit_dir:
            rows.append(("Журнал запросов", f"`{_rel(r.audit_dir)}`"))
        body = "\n".join(f"| {k} | {v} |" for k, v in rows)
        return f"## 1. Паспорт прогона\n\n| Поле | Значение |\n| :--- | :--- |\n{body}"

    def segmentation(self) -> str:
        r = self.res
        kinds = Counter(KIND.get(x.kind, x.kind) for x in r.requirements)
        lines = [
            "## 2. Как агент нарезал требования",
            "",
            f"Агент выделил требований: **{len(r.requirements)}** ("
            + ", ".join(f"{k}: {v}" for k, v in kinds.most_common()) + "). "
            "Если у требования в документе нет номера, агент присвоил локатор по разделу. "
            "Составные пункты оставлены целиком, их атомарность проверяется критерием «Единичность».",
            "",
            "| ID в отчёте | ID в документе | Где в документе | Краткая формулировка | Цитата |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]
        for q in r.requirements:
            lines.append(
                f"| {q.rid} | {self.cell(q.doc_id)} | {self.cell(q.anchor.where())} | {self.cell(q.summary)} | "
                f"«{self.cell(q.quote, 160, model=False)}» |"
            )
        if r.skipped:
            lines += ["", "**Не отнесено к требованиям:**", ""]
            lines += [f"* {self.cell(s)}: {self.cell(why)}" for s, why in r.skipped]
        if r.lost_requirements:
            lines += ["", "**Отброшено при нарезке** (модель привела цитату, которой нет в документе):", ""]
            lines += [f"* {self.cell(x, 200)}" for x in r.lost_requirements]
        return "\n".join(lines)

    def _finding(self, f: Finding, heading: bool = False) -> str:
        crit = ALL[f.criterion]
        reqs = ", ".join(f"{x} ({self.cell(self.res.req(x).doc_id)})" for x in f.req_ids if self.res.req(x))
        src = "правило" if f.source == "rule" else "LLM"
        out = [
            f'<a id="{f.fid.lower()}"></a>**{f.fid}** {SEV_ICON[f.severity]} **{SEVERITIES[f.severity]}** · '
            f"{crit.name} ({crit.source}) · источник: {src}" + (f" · {reqs}" if reqs and heading else ""),
            "",
            f"* **Проблема:** {self.t(f.problem)}",
        ]
        for a in f.anchors:
            out.append(f"* **Место:** {self.quote(a)}")
        out.append(f"* **Как исправить:** {self.t(f.fix)}")
        dups = [d for d in self.res.findings if d.kept and d.duplicate_of == f.fid]
        if dups:
            out.append("* **Совпадает с:** " + ", ".join(d.fid for d in dups) + " (объединены верификатором)")
        return "\n".join(out)

    def per_requirement(self) -> str:
        r = self.res
        head = "| ТР | ID в док. | " + " | ".join(SHORT[c.code] for c in REQUIREMENT_CRITERIA) + " |"
        sep = "| :--- | :--- |" + " :---: |" * len(REQUIREMENT_CRITERIA)
        rows = []
        for q in r.requirements:
            v = r.req_verdicts.get(q.rid, {})
            rows.append(
                f"| [{q.rid}](#{q.rid.lower()}) | {self.cell(q.doc_id, 28)} | "
                + " | ".join(MARK.get(v.get(c.code, ("ok", ""))[0], "✅") for c in REQUIREMENT_CRITERIA) + " |"
            )
        legend = (
            "Обозначения: ✅ соответствует, ❌ нарушение (см. находки), · не применимо. Столбцы: "
            + "; ".join(f"{SHORT[c.code]} = {c.name} ({c.source})" for c in REQUIREMENT_CRITERIA) + "."
        )
        out = ["## 3. Проверка каждого требования", "", "### 3.1. Матрица критериев", "", head, sep, *rows, "", legend, "",
               "### 3.2. Находки по требованиям"]
        kept = r.kept()
        for q in r.requirements:
            own = [f for f in kept if f.scope == "requirement" and q.rid in f.req_ids]
            related = [f for f in kept if f.scope == "set" and q.rid in f.req_ids]
            out += ["", f'<a id="{q.rid.lower()}"></a>', f"#### {q.rid} · {self.cell(q.doc_id)} · {self.cell(q.summary)}", "",
                    f"> {self.cell(q.quote, 400, model=False)}  ", f"> *{q.anchor.where()}*", ""]
            if not own:
                out.append("Нарушений на уровне отдельного требования не подтверждено.")
            for f in own:
                out += [self._finding(f), ""]
            if related:
                out.append("Связанные находки уровня набора: " + ", ".join(self.fid_link(f.fid) for f in related) + ".")
            oks = [(c, v) for c, v in r.req_verdicts.get(q.rid, {}).items() if v[0] == "na" and v[1]]
            if oks:
                out.append("Не применимо: " + "; ".join(f"{ALL[c].name}: {self.cell(v[1], 120)}" for c, v in oks) + ".")
        return "\n".join(out)

    def set_level(self) -> str:
        r = self.res
        out = ["## 4. Проверка всего набора требований", "", "| Критерий | Источник | Оценка | Комментарий |",
               "| :--- | :--- | :--- | :--- |"]
        for c in SET_CRITERIA:
            v, comment = r.set_verdicts.get(c.code, ("ok", ""))
            ids = [f.fid for f in r.kept() if f.criterion == c.code]
            note = self.cell(comment, 300)
            if ids:
                note += (" " if note else "") + "Находки: " + ", ".join(ids) + "."
            out.append(f"| {c.name} | {c.source} | {SET_MARK.get(v, v)} | {note} |")
        kept_set = [f for f in r.kept() if f.scope == "set"]
        order = [c.code for c in SET_CRITERIA] + [c.code for c in REQUIREMENT_CRITERIA]
        for code in order:
            fs = [f for f in kept_set if f.criterion == code]
            if not fs:
                continue
            out += ["", f"### {ALL[code].name}"]
            for f in fs:
                out += ["", self._finding(f, heading=True)]
        return "\n".join(out)

    def questions(self) -> str:
        r = self.res
        if not r.questions:
            return ""
        from .grounding import Grounder

        g = Grounder(r.doc, r.redactor.redact(r.doc.text) if r.redactor and r.config.redact else r.doc.text)
        out = ["## 5. Вопросы для уточнения", "",
               "Вопросы, которые нужно закрыть с заказчиком и смежными командами до старта разработки. "
               "Для каждого предложены варианты ответа."]
        n = 0
        for pr in ("blocking", "important", "nice"):
            qs = [q for q in r.questions if q.priority == pr]
            if not qs:
                continue
            out += ["", f"### {PRIORITY[pr]}"]
            for q in qs:
                n += 1
                a = g.find(q.quote) if q.quote else None
                links = [x for x in q.finding_ids if x in self.by_id]
                out += ["", f"**В-{n}. {self.t(q.question)}**", ""]
                out.append(f"* **Кому:** {self.cell(q.addressee)}")
                if a:
                    out.append(f"* **Контекст:** {self.quote(a)}")
                out.append(f"* **Почему важно:** {self.t(q.why)}")
                if q.options:
                    out.append("* **Варианты ответа:** " + "; ".join(f"({i}) {self.t(o)}" for i, o in enumerate(q.options, 1)))
                if links:
                    out.append("* **Связанные находки:** " + ", ".join(self.fid_link(x) for x in links))
        return "\n".join(out)

    def summary(self) -> str:
        r = self.res
        kept = r.kept()
        by_sev = Counter(f.severity for f in kept)
        rejected = [f for f in r.findings if not f.kept]
        dups = [f for f in r.findings if f.kept and f.duplicate_of]
        out = [
            "## 6. Сводка",
            "",
            f"* Требований выделено: **{len(r.requirements)}**.",
            f"* Требований с нарушениями на уровне отдельного требования: "
            f"**{len({x for f in kept if f.scope == 'requirement' for x in f.req_ids})}** из {len(r.requirements)}.",
            f"* Подтверждённых нарушений: **{len(kept)}** (🔴 критичных {by_sev['critical']}, 🟠 существенных "
            f"{by_sev['major']}, 🟡 незначительных {by_sev['minor']}).",
            f"* Отклонено верификатором: {len(rejected)}; объединено дублей: {len(dups)}; "
            f"отброшено без опоры на текст: {len(r.ungrounded)}.",
            f"* Вопросов для уточнения: {len(r.questions)} (блокирующих {sum(1 for q in r.questions if q.priority == 'blocking')}).",
            "",
            "| Критерий | 🔴 | 🟠 | 🟡 | Всего |",
            "| :--- | :---: | :---: | :---: | :---: |",
        ]
        cnt = Counter((f.criterion, f.severity) for f in kept)
        for c in REQUIREMENT_CRITERIA + SET_CRITERIA:
            tot = sum(cnt[(c.code, s)] for s in SEVERITIES)
            if tot:
                out.append(f"| {c.name} | {cnt[(c.code, 'critical')]} | {cnt[(c.code, 'major')]} | {cnt[(c.code, 'minor')]} | {tot} |")
        groups = {
            "Противоречивые": ("set_consistent", "correct"),
            "Непроверяемые": ("verifiable",),
            "Неполные": ("complete", "set_complete"),
        }
        crit = [f for f in kept if f.severity == "critical"]
        out += ["", "### Критичные дефекты"]
        if not crit:
            out.append("")
            out.append("Критичных дефектов не найдено.")
        used = set()
        for title, codes in list(groups.items()) + [("Прочие", None)]:
            fs = [f for f in crit if (codes is None and f.fid not in used) or (codes and f.criterion in codes)]
            if not fs:
                continue
            out += ["", f"**{title}:**", ""]
            for f in fs:
                used.add(f.fid)
                reqs = ", ".join(f"{x} ({self.cell(r.req(x).doc_id)})" for x in f.req_ids if r.req(x)) or "документ"
                line = f.anchors[0].where() if f.anchors else ""
                out.append(f"* {self.fid_link(f.fid)} · {reqs} · {line}: {self.cell(_first_sentence(f.problem), 260)}")
        return "\n".join(out)

    def verdict(self) -> str:
        r = self.res
        c = r.conclusion
        out = ["## 7. Итог", "", f"**Можно ли начинать разработку:** {r.decision_dev}."]
        if c:
            out.append(f"{self.t(c.development_rationale)}")
        out += ["", f"**Можно ли начинать тестирование:** {r.decision_test}."]
        if c:
            out.append(f"{self.t(c.testing_rationale)}")
            out += ["", "**Что исправить в первую очередь:**", ""]
            for i, it in enumerate(c.fix_first, 1):
                ids = ", ".join(self.fid_link(x) for x in it.finding_ids if x in self.by_id)
                out.append(f"{i}. **{self.t(it.title)}.** {self.t(it.action)}" + (f" ({ids})" if ids else ""))
            out += ["", f"**Общий вывод.** {self.t(c.overall)}"]
        out += ["", "Правило решения: есть хотя бы один критичный дефект, значит начинать нельзя; есть только существенные, "
                "значит можно с оговорками. Решение принимает код, а не модель."]
        return "\n".join(out)

    def appendix(self) -> str:
        r = self.res
        out = [
            "## Приложение A. Как работает проверка",
            "",
            "1. Документ прочитан и приведён к тексту с номерами строк; чувствительные данные (карты, ИИН, IBAN, "
            "телефоны, e-mail, токены, ФИО) заменены плейсхолдерами до отправки в модель, исходные значения "
            "возвращены только в этом отчёте.",
            "2. Модель выделила требования; каждая цитата найдена в документе (точно или нечётко), иначе требование отброшено.",
            "3. Детерминированные правила проверили размытые слова, пустые поля шаблона, версию, приоритеты, единицы, "
            "семантику HTTP и коды ответа.",
            "4. Модель проверила каждое требование по 9 критериям ISO/IEC/IEEE 29148 п. 5.2.5 и набор по критериям п. 5.2.6 "
            "и IEEE 830 п. 4.3 (два отдельных прохода: противоречия; полнота и прочие свойства).",
            "5. Находки без цитаты из документа отброшены. Остальные проверил независимый проход модели: отклонил домыслы, "
            "откалибровал серьёзность, объединил дубли.",
            "6. По подтверждённым находкам модель составила вопросы для уточнения и итог; решение о готовности принимает код.",
            "",
            "**Шкала серьёзности.**",
            "",
            *[f"* {ln}" for ln in SEVERITY_RULES.replace("\\\n", "").split("\n")],
        ]
        rejected = [f for f in r.findings if not f.kept]
        if rejected:
            out += ["", "## Приложение B. Отклонённые верификатором находки", "",
                    "Показаны для прозрачности: эти находки не учтены в сводке.", "",
                    "| ID | Критерий | Находка | Причина отклонения |", "| :--- | :--- | :--- | :--- |"]
            for f in rejected:
                out.append(f"| <a id=\"{f.fid.lower()}\"></a>{f.fid} | {ALL[f.criterion].name} | "
                           f"{self.cell(_first_sentence(f.problem), 200)} | {self.cell(f.verifier_note, 200)} |")
        dups = [f for f in r.findings if f.kept and f.duplicate_of]
        if dups:
            out += ["", "## Приложение C. Объединённые дубли", "", "| ID | Дубль находки | Формулировка |", "| :--- | :--- | :--- |"]
            for f in dups:
                out.append(f"| <a id=\"{f.fid.lower()}\"></a>{f.fid} | {self.fid_link(f.duplicate_of)} | "
                           f"{self.cell(_first_sentence(f.problem), 200)} |")
        return "\n".join(out)


def _first_sentence(s: str) -> str:
    s = (s or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    return m.group(1) if m and len(m.group(1)) > 25 else s


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(Path.cwd().resolve())).replace("\\", "/")
    except ValueError:
        return str(p)


def write(res: RunResult, out_path: str | Path) -> Path:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(Renderer(res).render(), encoding="utf-8")
    return p
