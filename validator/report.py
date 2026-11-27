"""Отчёт о проверке в формате Markdown (на русском)."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .config import PROVIDERS
from .criteria import SEVERITIES, SEVERITY_RULES, Criterion
from .grounding import Anchor
from .pipeline import RunResult
from .schemas import Finding

# без эмодзи: статус передаётся словом, а не значком или цветом
MARK = {"ok": "", "violation": "×", "na": "н/п", "unreviewed": "?"}
SET_MARK = {"ok": "соответствует", "partial": "частично", "violation": "нарушено", "unreviewed": "не проверено"}
ACCEPTANCE_MARK = {"agrees": "согласуется", "conflict": "противоречие", "unclear": "требует уточнения",
                   "unreviewed": "не проверено"}
SEV_SHORT = {"critical": "крит.", "major": "сущ.", "minor": "незн."}
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
    """Отчёт: сначала решение и что исправить, затем доказательства, длинные таблицы в конце.

    Каждый факт выводится один раз. Разделы названы как в ТЗ (паспорт, нарезка, проверка каждого
    требования, проверка набора, сводка, итог), чтобы их было легко найти.
    """

    def __init__(self, res: RunResult):
        self.res = res
        self.R = res.redactor.restore if res.redactor else (lambda s: s)
        self.by_id = {f.fid: f for f in res.findings}
        self.P = res.prof  # стандарты проверки и их критерии
        self.qnum = {}  # ID находки -> номер первого вопроса о ней
        for i, q in enumerate(res.questions, 1):
            for x in q.finding_ids:
                self.qnum.setdefault(x, i)

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
        return _clip(s, limit) if limit else s

    def quote(self, a: Anchor, limit: int = 220) -> str:
        q = self.cell(a.text, limit, model=False)
        return f"«{q}» ({_where(a)})"

    def items(self, xs, limit: int = 160) -> str:
        """Перечень через точку с запятой, без «.;» на стыках."""
        return "; ".join(self.cell(x, limit).rstrip(". ") for x in xs if x and x.strip())

    def fid_link(self, fid: str) -> str:
        return f"[{fid}](#{fid.lower()})"

    def reqs(self, f: Finding) -> str:
        ids = [f"{x} ({self.cell(self.res.req(x).doc_id)})" for x in f.req_ids if self.res.req(x)]
        return ", ".join(ids) or "документ целиком"

    # ---- разделы ----

    def render(self) -> str:
        parts = [
            self.header(),
            self.passport(),
            self.verdict(),
            self.summary(),
            self.questions(),
            self.set_level(),
            self.per_requirement(),
            self.checklist(),
            self.segmentation(),
            self.appendix(),
        ]
        return "\n\n".join(p.strip("\n") for p in parts if p) + "\n"

    def header(self) -> str:
        r = self.res
        open_ = r.open_findings()
        sev = Counter(f.severity for f in open_)
        closed = len(r.kept()) - len(open_)
        blocking = sum(1 for q in r.questions if q.priority == "blocking")
        out = [
            "# Отчёт о проверке требований",
            "",
            f"**{self.cell(r.doc.title, model=False)}**",
            "",
            f"> **Разработка:** {r.decision_dev}.  ",
            f"> **Тестирование:** {r.decision_test}.",
            "",
            "| Требований | Нарушений | Критичных | Существенных | Незначительных | Вопросов заказчику |",
            "| :---: | :---: | :---: | :---: | :---: | :---: |",
            f"| {len(r.requirements)} | {len(open_)} | {sev['critical']} | {sev['major']} | {sev['minor']} | "
            f"{len(r.questions)} (блокирующих {blocking}) |",
        ]
        if closed:
            out += ["", f"Ещё {closed} нарушений закрыто ответами заказчика."]
        out += ["", "Отчёт сформирован агентом автоматически. Каждое нарушение ссылается на страницу и строку документа "
                "и приводит дословную цитату."]
        return "\n".join(out)

    def passport(self) -> str:
        r = self.res
        u = r.usage
        provider = getattr(r.config, "provider", "gemini")
        model = f"{PROVIDERS.get(provider, {}).get('title', provider)} `{r.config.model}`"
        if u and u.steps:
            used = Counter(m for _, m in u.steps)
            if set(used) != {r.config.model}:
                model += "; ответили: " + ", ".join(f"`{m}` ({n} шаг.)" for m, n in used.most_common())
        dur = ""
        if r.finished:
            secs = int((r.finished - r.started).total_seconds())
            dur = f"{secs // 60} мин {secs % 60} с"
        rows = [
            ("Документ", f"{self.cell(r.doc.title, model=False)} (`{r.doc.path.name}`, {len(r.doc.lines)} строк, "
                         f"SHA-256 `{r.doc.sha256[:12]}`)"),
            ("Дата", r.started.strftime("%d.%m.%Y %H:%M") + f" ({r.config.tz.key})"),
            ("Модель", model),
            ("Версия агента", r.agent_version),
            ("Стандарты", self.P.cites()),
        ]
        if dur:
            req = f"; запросов к модели: {u.calls}" if u else ""
            rows.append(("Длительность", dur + req))
        for w in getattr(r, "warnings", []) or []:
            rows.append(("Предупреждение", self.cell(w, 300)))
        body = "\n".join(f"| {k} | {v} |" for k, v in rows)
        return f"## Паспорт прогона\n\n| | |\n| :--- | :--- |\n{body}"

    def verdict(self) -> str:
        r = self.res
        c = r.conclusion
        out = ['<a id="итог"></a>', "## Итог", ""]
        if c:  # само решение уже в рамке вверху отчёта: здесь только обоснование
            out += [f"**Разработка.** {self.t(c.development_rationale)}", "",
                    f"**Тестирование.** {self.t(c.testing_rationale)}"]
        else:
            out += [f"Разработка: {r.decision_dev}. Тестирование: {r.decision_test}."]
        if c and c.fix_first:
            out += ["", "### Что исправить в первую очередь", ""]
            for i, it in enumerate(c.fix_first, 1):
                ids = ", ".join(self.fid_link(x) for x in it.finding_ids if x in self.by_id)
                out.append(f"{i}. **{self.t(it.title).rstrip('.')}.** {self.t(it.action)}" + (f" ({ids})" if ids else ""))
        if c and c.overall:
            out += ["", self.t(c.overall)]
        return "\n".join(out)

    def summary(self) -> str:
        r = self.res
        kept = r.open_findings()
        crit = [f for f in kept if f.severity == "critical"]
        affected = len({x for f in kept if f.scope == "requirement" for x in f.req_ids})
        out = ['<a id="сводка"></a>', "## Сводка", "",
               f"Требований: **{len(r.requirements)}**, с нарушениями: **{affected}**. Открытых нарушений: **{len(kept)}**. "
               f"Ниже критичные дефекты; полный список в разделах проверки набора и каждого требования."]
        if not crit:
            return "\n".join(out + ["", "Критичных дефектов не найдено."])
        groups = [("Противоречивые", ("set_consistent", "correct")), ("Непроверяемые", ("verifiable",)),
                  ("Неполные", ("complete", "set_complete")), ("Прочие", None)]
        used: set[str] = set()
        for title, codes in groups:
            fs = [f for f in crit if f.fid not in used and (codes is None or f.criterion in codes)]
            if not fs:
                continue
            out += ["", f"### {title}", ""]
            for f in fs:
                used.add(f.fid)
                nums = sorted({a.line for a in f.anchors})[:4]
                lines = ("строка " if len(nums) == 1 else "строки ") + ", ".join(map(str, nums))
                ids = ", ".join(dict.fromkeys(self.cell(self.res.req(x).doc_id) for x in f.req_ids if self.res.req(x)))
                q = self.qnum.get(f.fid)
                ask = f" Вопрос [В-{q}](#в-{q})." if q else ""
                out.append(f"* **{self.fid_link(f.fid)}** {ids or 'документ'} / {lines}. "
                           f"{self.cell(_first_sentence(f.problem), 240)}{ask}")
        return "\n".join(out)

    def questions(self) -> str:
        r = self.res
        if not r.questions and not r.answers:
            return ""
        out = ["## Вопросы для уточнения", "",
               "Без ответов на блокирующие вопросы начинать разработку нельзя. Файл с полями для ответов лежит рядом "
               "с отчётом (`*_questions.md`); после ответов повторите проверку с `--answers`."]
        n = 0
        for pr in ("blocking", "important", "nice"):
            qs = [q for q in r.questions if q.priority == pr]
            if not qs:
                continue
            out += ["", f"### {PRIORITY[pr]}", ""]
            for q in qs:
                n += 1
                links = ", ".join(self.fid_link(x) for x in q.finding_ids if x in self.by_id)
                meta = " / ".join(x for x in (self.cell(q.addressee), links) if x)
                out.append(f'<a id="в-{n}"></a>**В-{n}.** {self.t(q.question)}' + (f" *({meta})*" if meta else "") + "  ")
                if q.options:
                    out.append("Варианты: " + "; ".join(f"({i}) {self.cell(o).rstrip('. ')}"
                                                        for i, o in enumerate(q.options, 1)) + ".")
                out.append("")
        if r.answers:
            out += ["### Учтённые ответы заказчика", ""]
            for qa in r.answers:
                closed = [f for f in r.kept() if f.resolution and qa.qid in f.resolution_note]
                eff = ", ".join(f"{self.fid_link(f.fid)} {'закрыта' if f.resolution == 'closed' else 'уточнена'}" for f in closed)
                out.append(f"* **{qa.qid}.** {self.cell(qa.question, 200)} Ответ: {self.cell(qa.answer, 300, model=False)}. "
                           + (f"Влияние: {eff}." if eff else "Дефекты не закрыты."))
        return "\n".join(out)

    def _finding(self, f: Finding, show_reqs: bool = True) -> str:
        crit = self.P.by_code.get(f.criterion) or Criterion(f.criterion, self.P.name(f.criterion), "", "")
        head = f'<a id="{f.fid.lower()}"></a>**{f.fid}** / {SEVERITIES[f.severity]} / {crit.name}'
        if show_reqs:
            head += f" / {self.reqs(f)}"
        out = []
        if f.resolution == "closed":
            out.append(f"*Закрыто ответом заказчика: {self.t(f.resolution_note)}*  ")
        elif f.resolution == "changed":
            out.append(f"*Уточнено ответом заказчика: {self.t(f.resolution_note)}*  ")
        out += [head + "  ", self.t(f.problem), ""]
        out += [f"> {self.quote(a)}  " for a in f.anchors]
        out += ["", f"**Исправить:** {self.t(f.fix)}"]
        return "\n".join(out)

    def set_level(self) -> str:
        r = self.res
        out = ["## Проверка всего набора требований", "", "| Критерий | Оценка | Комментарий |", "| :--- | :--- | :--- |"]
        for c in self.P.set:
            v, comment = r.set_verdicts.get(c.code, ("unreviewed", ""))
            ids = [self.fid_link(f.fid) for f in r.open_findings() if f.criterion == c.code]
            note = self.cell(comment, 220)
            if ids:
                note += (" " if note else "") + "Находки: " + ", ".join(ids) + "."
            mark = SET_MARK.get(v, v)
            out.append(f"| {c.name} | {'**' + mark + '**' if v == 'violation' else mark} | {note} |")
        checks = getattr(r, "acceptance_checks", [])
        if checks:
            out += ["", "### Сверка примеров приёмки с правилами", "",
                    "| Пример | Правило | Результат | По правилу | В примере |", "| :--- | :--- | :--- | :--- | :--- |"]
            for c in checks:
                example, rule = r.req(c.acceptance_id), r.req(c.req_id)
                a = f"{self.cell(example.doc_id)}, строка {example.anchor.line}"
                b = f"{self.cell(rule.doc_id)}, строка {rule.anchor.line}" if rule else "не найдено"
                res_ = ACCEPTANCE_MARK[c.status]
                out.append(f"| {a} | {b} | {'**' + res_ + '**' if c.status == 'conflict' else res_} | "
                           f"{self.cell(c.expected, 160)} | {self.cell(c.stated, 160)} |")
        if r.coverage or checks:
            coverage = {c.req_id: c for c in r.coverage}
            statuses = {(c.req_id, c.acceptance_id): c.status for c in checks}
            out += ["", "### Покрытие функциональных требований критериями приёмки", "",
                    "| Требование | Проверяется примерами | Не покрыто |", "| :--- | :--- | :--- |"]
            for q in (q for q in r.requirements if q.kind == "functional"):
                c = coverage.get(q.rid)
                linked = list(dict.fromkeys([*(c.acceptance_ids if c else []),
                                             *(aid for rid, aid in statuses if rid == q.rid)]))
                if not c and not linked:
                    continue  # описания архитектуры и схем: сценариев приёмки у них нет
                acc = ", ".join(f"{aid} ({ACCEPTANCE_MARK[statuses.get((q.rid, aid), 'unreviewed')]})"
                                for aid in linked) or "нет"
                miss = (self.items(c.missing_scenarios, 140) or "нет") if c else "не проверено"
                out.append(f"| {self.cell(q.doc_id)} | {acc} | {miss} |")
        kept_set = [f for f in r.kept() if f.scope == "set"]
        if kept_set:
            out += ["", "### Нарушения уровня набора"]
            order = [c.code for c in self.P.set] + [c.code for c in self.P.requirement]
            rank = {"critical": 0, "major": 1, "minor": 2}
            kept_set.sort(key=lambda f: (rank[f.severity], order.index(f.criterion) if f.criterion in order else 99))
            for f in kept_set:
                out += ["", self._finding(f)]
        return "\n".join(out)

    def per_requirement(self) -> str:
        r = self.res
        head = "| ТР | В документе | " + " | ".join(c.column for c in self.P.requirement) + " |"
        sep = "| :--- | :--- |" + " :---: |" * len(self.P.requirement)
        rows = []
        for q in r.requirements:
            v = r.req_verdicts.get(q.rid, {})
            rows.append(f"| {q.rid} | {self.cell(q.doc_id, 28)} | "
                        + " | ".join(MARK.get(v.get(c.code, ("unreviewed", ""))[0], "?") for c in self.P.requirement) + " |")
        legend = ("× нарушение, н/п не применимо, ? не проверено, пусто: соответствует. "
                  + "; ".join(f"{c.column}: {c.name}" for c in self.P.requirement) + ".")
        out = ["## Проверка каждого требования", "", head, sep, *rows, "", legend]
        kept = [f for f in r.kept() if f.scope == "requirement"]
        for q in r.requirements:
            own = [f for f in kept if q.rid in f.req_ids and f.req_ids[0] == q.rid]
            if not own:
                continue
            out += ["", f'<a id="{q.rid.lower()}"></a>', f"### {q.rid} / {self.cell(q.doc_id)}: {self.cell(q.summary)}"]
            for f in own:
                out += ["", self._finding(f, show_reqs=len(f.req_ids) > 1)]
        return "\n".join(out)

    def checklist(self) -> str:
        r = self.res
        if not r.checklist:
            return ""
        icon = {"да": "Да", "нет": "**Нет**", "не требуется": "Не требуется"}
        yes = sum(1 for c in r.checklist if c.answer.strip().lower() == "да")
        out = ["## Чек-лист ревью требований", "",
               f"В документе есть чек-лист ревью банка без отметок. Агент заполнил его по результатам проверки: "
               f"выполнено {yes} из {len(r.checklist)}.", "",
               "| Пункт | Ответ | Обоснование |", "| :--- | :--- | :--- |"]
        for c in r.checklist:
            ids = ", ".join(self.fid_link(x) for x in c.finding_ids if x in self.by_id)
            out.append(f"| {self.cell(c.item, model=False)} | {icon.get(c.answer.strip().lower(), self.cell(c.answer))} | "
                       f"{self.cell(c.rationale, 220)}" + (f" ({ids})" if ids else "") + " |")
        return "\n".join(out)

    def segmentation(self) -> str:
        r = self.res
        kinds = Counter(KIND.get(x.kind, x.kind) for x in r.requirements)
        lines = [
            "## Как агент нарезал требования",
            "",
            f"Выделено требований: **{len(r.requirements)}** (" + ", ".join(f"{k} {v}" for k, v in kinds.most_common()) + "). "
            "Где в документе нет номера, агент присвоил локатор по разделу и сохранил цитату.",
            "",
            "| ID в отчёте | ID в документе | Краткая формулировка | Цитата |",
            "| :--- | :--- | :--- | :--- |",
        ]
        for q in r.requirements:
            lines.append(f"| {q.rid} | {self.cell(q.doc_id)} | {self.cell(q.summary)} | "
                         f"«{self.cell(q.quote, 110, model=False)}» ({_where(q.anchor)}) |")
        if r.skipped:
            lines += ["", "Не отнесено к требованиям: " + "; ".join(f"{self.cell(s)} ({self.cell(why)})" for s, why in r.skipped) + "."]
        if r.lost_requirements:
            lines += ["", "Отброшено при нарезке (цитаты нет в документе): "
                      + "; ".join(self.cell(x, 120) for x in r.lost_requirements) + "."]
        return "\n".join(lines)

    def appendix(self) -> str:
        r = self.res
        u = r.usage
        red = r.redactor.stats() if r.redactor else {}
        if not r.config.redact:
            red_txt = "отключено"
        elif red:
            red_txt = "заменено до отправки в модель: " + ", ".join(f"{k} {v}" for k, v in red.items())
        else:
            red_txt = "включено, чувствительных данных не найдено"
        out = [
            "## Приложение: как работает проверка",
            "",
            "1. Документ приведён к тексту с номерами строк; карты, ИИН, IBAN, телефоны, e-mail, токены и ФИО "
            f"заменяются плейсхолдерами до отправки в модель ({red_txt}).",
            "2. Требования выделены (размеченные пункты кодом, остальные моделью), каждая цитата найдена в документе.",
            "3. Правила в коде проверили размытые слова, пустые поля шаблона, версию, приоритеты, единицы, методы HTTP и коды ответа.",
            f"4. Модель проверила каждое требование по {len(self.P.requirement)} критериям и набор по {len(self.P.set)} "
            "критериям двумя проходами (противоречия; полнота и прочее). Даты в примерах приёмки сверил код.",
            "5. Находки без цитаты отброшены, остальные проверил независимый проход модели; одинаковые находки объединены.",
            "6. Решение о готовности принимает правило в коде: критичный дефект блокирует затронутую работу.",
        ]
        if u:
            pc = getattr(u, "prompt_cached_tokens", 0)
            out.append(f"\nЗапросов к модели: {u.calls}; токенов: вход {u.prompt_tokens}"
                       + (f" (из кэша провайдера {pc})" if pc else "") + f", выход {u.output_tokens}"
                       + (f", рассуждение {u.thinking_tokens}" if u.thinking_tokens else "")
                       + (f"; шагов из локального кэша: {u.cached}" if getattr(u, "cached", 0) else "")
                       + (f". Журнал запросов: `{_rel(r.audit_dir)}`." if r.audit_dir else "."))
        out += ["", "**Шкала серьёзности.**", "", *[f"* {ln}" for ln in SEVERITY_RULES.replace("\\\n", "").split("\n")]]
        rejected = [f for f in r.findings if not f.kept]
        dups = [f for f in r.findings if f.kept and f.duplicate_of]
        if rejected:
            out += ["", "### Отклонено верификатором", "", "| ID | Находка | Почему отклонена |", "| :--- | :--- | :--- |"]
            for f in rejected:
                out.append(f"| <a id=\"{f.fid.lower()}\"></a>{f.fid} | {self.cell(_first_sentence(f.problem), 160)} | "
                           f"{self.cell(f.verifier_note, 160)} |")
        if dups:
            out += ["", "### Объединённые дубли", "",
                    ", ".join(f'<a id="{f.fid.lower()}"></a>{f.fid} в {self.fid_link(f.duplicate_of)}' for f in dups) + "."]
        if r.fixed:
            out += ["", "### Исправленная редакция документа", "",
                    "Файл `*_fixed.md` рядом с отчётом. Где нужен выбор заказчика, стоит пометка [УТОЧНИТЬ: …].", "",
                    "| Что изменено | Находки |", "| :--- | :--- |"]
            for c in r.fixed.changes:
                ids = ", ".join(self.fid_link(x) for x in c.finding_ids if x in self.by_id)
                out.append(f"| {self.cell(c.change, 240)} | {ids} |")
            if r.fixed.open_points:
                out += ["", "Осталось решить заказчику: " + "; ".join(self.t(x) for x in r.fixed.open_points)]
        return "\n".join(out)


def _where(a: Anchor) -> str:
    """Место коротко: страница и строка (раздел виден по ID требования)."""
    return a.where().split(", раздел")[0]


def _clip(s: str, limit: int) -> str:
    """Обрезать по границе слова, а не посреди слова."""
    if len(s) <= limit:
        return s
    cut = s[: limit - 1]
    sp = cut.rfind(" ")
    if sp > limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:.") + "…"


def _first_sentence(s: str) -> str:
    s = (s or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    return m.group(1) if m and len(m.group(1)) > 25 else s


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(Path.cwd().resolve())).replace("\\", "/")
    except ValueError:
        return str(p)


def write_extras(res: RunResult, report_path: str | Path) -> dict[str, Path]:
    """Файлы рядом с отчётом: вопросы с полями для ответов и исправленная редакция."""
    from .clarify import write_questions

    rp = Path(report_path)
    out: dict[str, Path] = {}
    if res.questions:
        out["Вопросы заказчику (впишите ответы)"] = write_questions(res, rp.with_name(rp.stem + "_questions.md"))
    if res.fixed:
        restore = res.redactor.restore if res.redactor else (lambda x: x)
        fp = rp.with_name(rp.stem + "_fixed.md")
        fp.write_text(restore(res.fixed.markdown).strip() + "\n", encoding="utf-8")
        out["Исправленная редакция документа"] = fp
    return out


def write_diagnostic(input_path: str | Path, err: Exception, out_path: str | Path) -> Path:
    """Отчёт для документа, который нельзя проверить: что не так и что сделать, без запросов к модели."""
    from datetime import datetime

    from . import __version__

    src = Path(input_path)
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    problem = getattr(err, "problem", str(err))
    advice = getattr(err, "advice", "")
    try:
        size = f"{src.stat().st_size:,} байт".replace(",", " ") if src.is_file() else "нет"
    except OSError:
        size = "нет"
    lines = [
        f"# Отчёт о проверке требований: {src.name}",
        "",
        "**Проверка не выполнена: документ нельзя прочитать.**",
        "",
        "Разработка: нельзя. Проектирование тестов: нельзя. Требования оценить невозможно, пока нет читаемого текста.",
        "",
        "| Параметр | Значение |",
        "| :--- | :--- |",
        f"| Файл | {src.name} |",
        f"| Размер | {size} |",
        f"| Дата | {datetime.now().astimezone():%d.%m.%Y %H:%M} |",
        f"| Версия агента | {__version__} |",
        "",
        "## Что не так",
        "",
        problem,
        "",
    ]
    if advice:
        lines += ["## Что сделать", "", advice, ""]
    lines += [
        "## Что агент проверил перед разбором",
        "",
        "1. Файл существует, и это файл, а не папка.",
        "2. Формат поддерживается: PDF, DOCX, Markdown, TXT, HTML.",
        "3. Файл не пустой и действительно в своём формате (сигнатура PDF, архив DOCX, текст без двоичных данных).",
        "4. PDF не защищён паролем, не повреждён и содержит текстовый слой, а не только сканы.",
        "5. После чтения остаётся достаточно текста и есть хотя бы одна формулировка требования.",
        "",
        "Агент остановился на первой неудачной проверке. Запросы к модели не отправлялись: "
        "разбирать было нечего, поэтому данные документа никуда не передавались.",
        "",
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def write(res: RunResult, out_path: str | Path) -> Path:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(Renderer(res).render(), encoding="utf-8")
    return p
