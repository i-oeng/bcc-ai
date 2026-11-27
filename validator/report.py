"""Отчёт о проверке на русском: Markdown, из него PDF (validator/pdf.py)."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .config import PROVIDERS
from .criteria import SEVERITIES
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
    # Набор разделов фиксирован и выводится всегда, даже пустой: рамка решения, 1-6 и приложение А
    # (приложение Б только с --fix). Внутри разделов текст, цифры и таблицы не обрезаются.
    # Всё, что написала модель, к этому моменту прошло проверку опоры (strict.py).

    def render(self) -> str:
        parts = [
            self.header(),
            self.passport(),
            self.verdict(),
            self.summary(),
            self.questions(),
            self.set_level(),
            self.per_requirement(),
            self.segmentation(),
            self.fixed_doc(),
        ]
        return "\n\n".join(p.strip("\n") for p in parts if p) + "\n"

    def header(self) -> str:
        r = self.res
        closed = len(r.kept()) - len(r.open_findings())
        out = [
            "# Отчёт о проверке требований",
            "",
            f"**{self.cell(r.doc.title, model=False)}**",
            "",
            f"> **Разработка:** {r.decision_dev}.  ",
            f"> **Тестирование:** {r.decision_test}.",
        ]
        if closed:
            out += ["", f"Ещё {closed} нарушений закрыто ответами заказчика."]
        return "\n".join(out)

    def passport(self) -> str:
        r = self.res
        u = r.usage
        provider = getattr(r.config, "provider", "gemini")
        model = f"{PROVIDERS.get(provider, {}).get('title', provider)} {r.config.model}"
        if u and u.steps:
            used = Counter(m for _, m in u.steps)
            if set(used) != {r.config.model}:
                model += "; ответили: " + ", ".join(f"{m} ({n} шаг.)" for m, n in used.most_common())
        rows = [
            ("Документ", f"{self.cell(r.doc.title, model=False)} ({r.doc.path.name}, {len(r.doc.lines)} строк, "
                         f"SHA-256 {r.doc.sha256[:12]})"),
            ("Дата", r.started.strftime("%d.%m.%Y %H:%M") + f" ({_tz_ru(r.started, r.config.tz.key)})"),
            ("Модель", model),
            ("Версия агента", f"oresa {_version_ru(r.agent_version)}"),
            ("Стандарты", self.P.cites()),
        ]
        if r.finished:
            secs = int((r.finished - r.started).total_seconds())
            rows.append(("Длительность", f"{secs // 60} мин {secs % 60} с" + (f", запросов к модели: {u.calls}" if u else "")))
        red = r.redactor.stats() if r.redactor else {}
        if not r.config.redact:
            rows.append(("Маскирование", "отключено"))
        elif red:
            rows.append(("Маскирование", "до отправки в модель заменено: "
                         + ", ".join(f"{MASK_RU.get(k, k)}: {v}" for k, v in red.items())))
        else:
            rows.append(("Маскирование", "включено, чувствительных данных не найдено"))
        rows.append(("Опора на документ", self._grounding()))
        for w in getattr(r, "warnings", []) or []:
            rows.append(("Предупреждение", self.cell(w)))
        body = "\n".join(f"| {k} | {v} |" for k, v in rows)
        return f"## 1. Паспорт прогона\n\n| Параметр | Значение |\n| :--- | :--- |\n{body}"

    def _grounding(self) -> str:
        r = self.res
        g = getattr(r, "grounding", None) or {}
        parts = [f"находки без цитаты из документа: {len(r.ungrounded)}",
                 f"требования, цитата которых не найдена: {len(r.lost_requirements)}"]
        removed = [("ссылки на несуществующие нарушения, требования или вопросы", g.get("ids", 0)),
                   ("ссылки на строки вне документа", g.get("lines", 0)),
                   ("вопросы без связи с документом", g.get("questions", 0)),
                   ("цитаты в вопросах, которых нет в документе", g.get("quotes", 0)),
                   ("пункты итога без нарушений", g.get("fix_first", 0)),
                   ("формулировки сверки, заменённые цитатой документа", g.get("acceptance", 0))]
        parts += [f"{k}: {v}" for k, v in removed if v]
        return ("каждое нарушение, требование и вопрос в отчёте опирается на цитату, найденную в документе "
                "(совпадение не ниже 85%), номера строк и ссылки сверены кодом. Исключено при проверке: "
                + "; ".join(parts) + ".")

    def verdict(self) -> str:
        r = self.res
        c = r.conclusion
        out = ["## 2. Итог", ""]
        if c:  # само решение в рамке вверху отчёта, здесь обоснование модели
            out += [f"**Разработка.** {self.t(c.development_rationale)}", "",
                    f"**Тестирование.** {self.t(c.testing_rationale)}"]
        else:
            out += [f"Разработка: {r.decision_dev}. Тестирование: {r.decision_test}."]
        out += ["", "**Что исправить в первую очередь**", ""]
        if c and c.fix_first:
            for i, it in enumerate(c.fix_first, 1):
                ids = ", ".join(x for x in it.finding_ids if x in self.by_id)
                out.append(f"{i}. **{self.t(it.title).rstrip('.')}.** {self.t(it.action)}" + (f" ({ids})" if ids else ""))
        else:
            out.append("Открытых нарушений нет.")
        if c and c.overall:
            out += ["", self.t(c.overall)]
        return "\n".join(out)

    def summary(self) -> str:
        r = self.res
        kept = r.open_findings()
        sev = Counter(f.severity for f in kept)
        crit = [f for f in kept if f.severity == "critical"]
        affected = len({x for f in kept if f.scope == "requirement" for x in f.req_ids})
        blocking = sum(1 for q in r.questions if q.priority == "blocking")
        out = ["## 3. Сводка", "",
               "| Показатель | Значение |", "| :--- | :--- |",
               f"| Требований | {len(r.requirements)} |",
               f"| Требований с нарушениями | {affected} |",
               f"| Открытых нарушений | {len(kept)} |",
               f"| из них критичных | {sev['critical']} |",
               f"| из них существенных | {sev['major']} |",
               f"| из них незначительных | {sev['minor']} |",
               f"| Вопросов заказчику | {len(r.questions)} (блокирующих {blocking}) |", ""]
        out += self._metrics()
        out.append("**Критичные дефекты**")
        if not crit:
            return "\n".join(out + ["", "Критичных дефектов не найдено."])
        groups = [("Противоречивые", ("set_consistent", "correct")), ("Непроверяемые", ("verifiable",)),
                  ("Неполные", ("complete", "set_complete")), ("Прочие", None)]
        used: set[str] = set()
        for title, codes in groups:
            fs = [f for f in crit if f.fid not in used and (codes is None or f.criterion in codes)]
            if not fs:
                continue
            out += ["", f"*{title}*", ""]
            for f in fs:
                used.add(f.fid)
                out.append(f"* **{f.fid}** ({self._place(f)}). {self.cell(f.problem)}")
        return "\n".join(out)

    # ---- показатели и графики (считает код по подтверждённым данным, модель цифр не пишет) ----

    def _marks(self) -> dict[str, list[str]]:
        r = self.res
        return {q.rid: [r.req_verdicts.get(q.rid, {}).get(c.code, ("unreviewed", ""))[0] for c in self.P.requirement]
                for q in r.requirements}

    def _index(self, verdicts: list[str]) -> int | None:
        """Индекс качества требования: доля пройденных применимых критериев, в процентах."""
        ok, bad = verdicts.count("ok"), verdicts.count("violation")
        return round(100 * ok / (ok + bad)) if ok + bad else None

    def _metrics(self) -> list[str]:
        r = self.res
        kept = r.open_findings()
        marks = self._marks()
        ok = sum(v.count("ok") for v in marks.values())
        bad = sum(v.count("violation") for v in marks.values())
        out = ["**Показатели качества**", ""]
        if ok + bad:
            clean = sum(1 for v in marks.values() if "violation" not in v)
            out += [f"Индекс качества документа: **{round(100 * ok / (ok + bad))}%** (доля пройденных проверок "
                    f"«требование и критерий», {ok} из {ok + bad}). Требований без нарушений: {clean} из {len(marks)}.", ""]
        if len(kept) >= 3:
            sev = Counter(f.severity for f in kept)
            out += _chart("Нарушения по серьёзности", "Серьёзность",
                          [(SEVERITIES[s].lower(), sev[s]) for s in ("critical", "major", "minor")])
            by_crit = Counter(self.P.name(f.criterion) for f in kept)
            if len(by_crit) >= 2:
                out += _chart("Нарушения по критериям качества", "Критерий", by_crit.most_common())
            by_sec = Counter()
            for f in kept:
                rq = next((r.req(x) for x in f.req_ids if r.req(x)), None)
                sec = (rq.section if rq else "") or (f.anchors[0].section if f.anchors else "") or "документ целиком"
                by_sec[sec.split(" > ")[0].split(" / ")[0].strip() or "документ целиком"] += 1
            if len(by_sec) >= 2:
                out += _chart("Нарушения по разделам документа", "Раздел", by_sec.most_common())
        cov = {c.req_id: c for c in r.coverage}
        func = [q for q in r.requirements if q.kind == "functional" and q.rid in cov]
        if func:
            covered = sum(1 for q in func if cov[q.rid].acceptance_ids)
            full = sum(1 for q in func if cov[q.rid].acceptance_ids and not cov[q.rid].missing_scenarios)
            out += _chart("Покрытие функциональных требований критериями приёмки", "Покрытие",
                          [("покрыты полностью", full), ("покрыты частично", covered - full),
                           ("не покрыты", len(func) - covered)])
        return out

    def questions(self) -> str:
        r = self.res
        out = ["## 4. Вопросы к заказчику", ""]
        if not r.questions:
            out.append("Вопросов нет: документ не оставляет открытых решений для заказчика.")
        else:
            out.append("Блокирующие вопросы нужно закрыть до начала разработки. Чтобы агент учёл ответы, "
                       "запустите проверку с ключом `--questions-file`, впишите ответы в созданную форму "
                       "и повторите проверку с ключом `--answers`.")
        n = 0
        for pr in ("blocking", "important", "nice"):
            qs = [q for q in r.questions if q.priority == pr]
            if not qs:
                continue
            out += ["", f"**{PRIORITY[pr]}**", ""]
            for q in qs:
                n += 1
                parts = [f"{n}. **В-{n}.** {self.t(q.question)}"]
                if q.why:
                    parts.append(f"Почему важно: {self.t(q.why).rstrip('.')}.")
                if q.options:
                    parts.append("Варианты: " + "; ".join(f"({i}) {self.cell(o).rstrip('. ')}"
                                                         for i, o in enumerate(q.options, 1)) + ".")
                src = self._quote_place(q.quote)
                links = ", ".join(x for x in q.finding_ids if x in self.by_id)
                meta = "; ".join(x for x in (f"кому: {self.cell(q.addressee).rstrip('.')}" if q.addressee else "",
                                             f"нарушения: {links}" if links else "", src) if x)
                if meta:
                    parts.append(f"*({meta})*")
                out.append(" ".join(parts))
        if r.answers:
            out += ["", "**Учтённые ответы заказчика**", ""]
            for qa in r.answers:
                closed = [f for f in r.kept() if f.resolution and qa.qid in f.resolution_note]
                eff = ", ".join(f"{f.fid} {'закрыта' if f.resolution == 'closed' else 'уточнена'}" for f in closed)
                out.append(f"* **{qa.qid}.** {self.cell(qa.question)} Ответ: {self.cell(qa.answer, model=False)}. "
                           + (f"Влияние: {eff}." if eff else "Дефекты не закрыты."))
        return "\n".join(out)

    def _quote_place(self, quote: str) -> str:
        """Основание вопроса: дословная цитата документа и её строка."""
        if not quote:
            return ""
        if not hasattr(self, "_g"):
            from .strict import _grounder

            self._g = _grounder(self.res)
        a = self._g.find(quote, min_score=85)
        return f"основание: «{self.cell(_plain(a.text), model=False)}», {_where(a)}" if a else ""

    # ---- таблица нарушений ----

    def _place(self, f: Finding) -> str:
        """Где: ID требований в документе и строки."""
        ids = list(dict.fromkeys(self.cell(self.res.req(x).doc_id) for x in f.req_ids if self.res.req(x)))
        lines = sorted({a.line for a in f.anchors})
        pages = sorted({a.page for a in f.anchors if a.page})
        loc = ""
        if lines:
            loc = ("строка " if len(lines) == 1 else "строки ") + ", ".join(map(str, lines))
            if pages:
                loc = ("с. " + ", ".join(map(str, pages)) + ", ") + loc
        return ", ".join(x for x in (", ".join(ids) or "документ целиком", loc) if x)

    def _row(self, fs: list[Finding]) -> str:
        f = fs[0]
        sev = {"critical": "**КРИТИЧНО**", "major": "**существенно**"}.get(f.severity, "незначительно")
        num = ", ".join(x.fid for x in fs)
        if len(fs) == 1:
            where = self._place(f)
        else:  # одинаковые мелкие замечания в разных местах: одна строка, все места перечислены
            lines = sorted({a.line for x in fs for a in x.anchors})
            where = ", ".join(dict.fromkeys(self.cell(self.res.req(r).doc_id) for x in fs for r in x.req_ids
                                            if self.res.req(r)))
            where = (where + ", " if where else "") + "строки " + ", ".join(map(str, lines))
        problem = self.cell(f.problem)
        if len(fs) > 1:
            problem += f" То же ещё в {len(fs) - 1} местах."
        if f.resolution == "closed":
            problem = f"*Закрыто ответом заказчика: {self.cell(f.resolution_note)}* " + problem
        elif f.resolution == "changed":
            problem = f"*Уточнено ответом заказчика: {self.cell(f.resolution_note)}* " + problem
        anchors = [a for x in fs for a in x.anchors]
        seen: set[str] = set()
        for a in anchors:  # каждая цитата один раз, со строкой
            if a.text in seen:
                continue
            seen.add(a.text)
            problem += f"<br>*«{self.cell(_plain(a.text), model=False)}» ({_where(a)})*"
        crit = self.P.by_code.get(f.criterion)
        crit_name = crit.name if crit else self.P.name(f.criterion)
        return f"| **{num}**<br>{sev} | {where} | {self.cell(crit_name)} | {problem} | {self.cell(f.fix)} |"

    def _table(self, fs: list[Finding]) -> list[str]:
        rank = {"critical": 0, "major": 1, "minor": 2}
        fs = sorted(fs, key=lambda f: (rank[f.severity], f.fid))
        groups: list[list[Finding]] = []
        index: dict[tuple, list[Finding]] = {}
        for f in fs:
            if f.severity == "minor" and not f.resolution:
                key = (f.criterion, re.sub(r"\d+", "#", f.problem.strip()), re.sub(r"\d+", "#", f.fix.strip()))
                if key in index:
                    index[key].append(f)
                    continue
                index[key] = [f]
                groups.append(index[key])
            else:
                groups.append([f])
        return ["| № | Где | Критерий | Нарушение и цитата | Как исправить |", "| :--- | :--- | :--- | :--- | :--- |",
                *(self._row(g) for g in groups)]

    # ---- проверка набора и каждого требования ----

    def set_level(self) -> str:
        r = self.res
        out = ["## 5. Проверка всего набора требований", "", "| Критерий | Оценка | Комментарий |", "| :--- | :--- | :--- |"]
        for c in self.P.set:
            v, comment = r.set_verdicts.get(c.code, ("unreviewed", ""))
            ids = [f.fid for f in r.open_findings() if f.criterion == c.code]
            note = self.cell(comment) if comment else ""
            if ids:
                note += (" " if note else "") + "Нарушения: " + ", ".join(ids) + "."
            mark = SET_MARK.get(v, v)
            out.append(f"| {c.name} | {'**' + mark + '**' if v == 'violation' else mark} | {note} |")
        checks = getattr(r, "acceptance_checks", [])
        out += ["", "### Сверка критериев приёмки с правилами", ""]
        if not checks:
            out.append("Критериев приёмки, которые можно сверить с правилами, в документе нет.")
        else:
            bad = [c for c in checks if c.status != "agrees"]
            out += [f"Согласуются {len(checks) - len(bad)} из {len(checks)}.", "",
                    "| Критерий приёмки | Правило | Результат | По правилу | В примере |",
                    "| :--- | :--- | :--- | :--- | :--- |"]
            for c in checks:
                example, rule = r.req(c.acceptance_id), r.req(c.req_id)
                a = f"{self.cell(example.doc_id)}, строка {example.anchor.line}" if example else c.acceptance_id
                b = f"{self.cell(rule.doc_id)}, строка {rule.anchor.line}" if rule else "не найдено"
                res_ = ACCEPTANCE_MARK[c.status]
                out.append(f"| {a} | {b} | {'**' + res_ + '**' if c.status == 'conflict' else res_} | "
                           f"{self.cell(c.expected)} | {self.cell(c.stated)} |")
        coverage = {c.req_id: c for c in r.coverage}
        statuses = {(c.req_id, c.acceptance_id): c.status for c in checks}
        rows = []
        for q in (q for q in r.requirements if q.kind == "functional"):
            c = coverage.get(q.rid)
            linked = list(dict.fromkeys([*(c.acceptance_ids if c else []),
                                         *(aid for rid, aid in statuses if rid == q.rid)]))
            if not c and not linked:
                continue  # описания архитектуры и схем: сценариев приёмки у них нет
            acc = ", ".join(f"{self.cell(r.req(aid).doc_id) if r.req(aid) else aid}" for aid in linked) or "нет"
            miss = (self.items(c.missing_scenarios) or "нет") if c else "не проверено"
            rows.append(f"| {self.cell(q.doc_id)} | {acc} | {miss} |")
        out += ["", "### Покрытие функциональных требований критериями приёмки", ""]
        out += (["| Требование | Проверяется критериями | Не покрыто |", "| :--- | :--- | :--- |", *rows] if rows
                else ["Функциональных требований с критериями приёмки в документе нет."])
        kept_set = [f for f in r.kept() if f.scope == "set"]
        out += ["", "### Нарушения уровня набора", ""]
        out += self._table(kept_set) if kept_set else ["Нарушений уровня набора не найдено."]
        return "\n".join(out)

    def per_requirement(self) -> str:
        r = self.res
        crits = self.P.requirement
        marks = {q.rid: [MARK.get(r.req_verdicts.get(q.rid, {}).get(c.code, ("unreviewed", ""))[0], "?") for c in crits]
                 for q in r.requirements}
        out = ["## 6. Проверка каждого требования", "",
               f"Каждое из {len(r.requirements)} требований проверено по {len(crits)} критериям.", "",
               "| ТР | В документе | " + " | ".join(c.column for c in crits) + " | Индекс |",
               "| :--- | :--- |" + " :---: |" * len(crits) + " ---: |"]
        verdicts = self._marks()
        for q in r.requirements:
            ix = self._index(verdicts[q.rid])
            out.append(f"| {q.rid} | {self.cell(q.doc_id)} | " + " | ".join(marks[q.rid])
                       + f" | {'' if ix is None else str(ix) + '%'} |")
        out += ["", "× нарушение, ? не проверено, н/п не применимо, пусто: соответствует. Индекс: доля пройденных "
                "применимых критериев. "
                + "; ".join(f"{c.column}: {c.name}" for c in crits) + "."]
        kept = [f for f in r.kept() if f.scope == "requirement"]
        out += ["", "### Нарушения по требованиям", ""]
        out += self._table(kept) if kept else ["Нарушений в отдельных требованиях не найдено."]
        return "\n".join(out)

    def segmentation(self) -> str:
        r = self.res
        kinds = Counter(KIND.get(x.kind, x.kind) for x in r.requirements)
        lines = [
            "## Приложение А. Как агент нарезал требования",
            "",
            f"Выделено требований: {len(r.requirements)} (" + ", ".join(f"{k} {v}" for k, v in kinds.most_common()) + "). "
            "Где у пункта нет номера в документе, агент присвоил локатор по разделу. Цитата взята из документа дословно.",
            "",
            "| ID в отчёте | ID в документе | Краткая формулировка | Цитата из документа | Строка |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]
        for q in r.requirements:
            where = (f"с. {q.anchor.page}, " if q.anchor.page else "") + str(q.anchor.line)
            if q.anchor.line_end != q.anchor.line:
                where += f" и далее до {q.anchor.line_end}"
            lines.append(f"| {q.rid} | {self.cell(q.doc_id)} | {self.cell(q.summary)} | "
                         f"«{self.cell(_plain(q.quote), model=False)}» | {where} |")
        if r.skipped:
            lines += ["", "Не отнесено к требованиям: " + "; ".join(f"{self.cell(s)} ({self.cell(why).rstrip('.')})"
                                                                  for s, why in r.skipped) + "."]
        return "\n".join(lines)

    def fixed_doc(self) -> str:
        r = self.res
        if not r.fixed:
            return ""
        out = ["## Приложение Б. Исправленная редакция документа", "",
               "Файл `*_fixed.md` лежит рядом с отчётом. Где нужен выбор заказчика, стоит пометка [УТОЧНИТЬ: …].", "",
               "| Что изменено | Нарушения |", "| :--- | :--- |"]
        for c in r.fixed.changes:
            out.append(f"| {self.cell(c.change)} | {', '.join(x for x in c.finding_ids if x in self.by_id)} |")
        if r.fixed.open_points:
            out += ["", "Осталось решить заказчику: " + "; ".join(self.t(x) for x in r.fixed.open_points)]
        return "\n".join(out)


MASK_RU = {"КАРТА": "номера карт", "EMAIL": "адреса электронной почты", "IBAN": "счета IBAN", "ИИН": "ИИН",
           "БИН": "БИН", "ФИО": "ФИО", "ТЕЛЕФОН": "телефоны", "СЕКРЕТ": "токены и ключи",
           "URL": "внутренние адреса (URL)", "IP": "IP-адреса"}
TZ_RU = {"Asia/Almaty": "Алматы", "Asia/Qostanay": "Костанай", "Asia/Aqtobe": "Актобе", "Asia/Aqtau": "Актау",
         "Asia/Oral": "Уральск", "Asia/Atyrau": "Атырау", "Asia/Qyzylorda": "Кызылорда", "Asia/Tashkent": "Ташкент",
         "Asia/Bishkek": "Бишкек", "Europe/Moscow": "Москва", "Asia/Dubai": "Дубай", "UTC": "UTC"}


def _tz_ru(when, key: str) -> str:
    """Часовой пояс по-русски: «время Алматы, UTC+5» вместо «Asia/Almaty»."""
    off = when.utcoffset()
    sign = "+" if not off or off.total_seconds() >= 0 else "-"
    mins = abs(int(off.total_seconds() // 60)) if off else 0
    utc = "UTC" + (f"{sign}{mins // 60}" + (f":{mins % 60:02d}" if mins % 60 else "") if mins else "")
    city = TZ_RU.get(key)
    return f"время {city}, {utc}" if city and city != "UTC" else (utc if city else f"{key}, {utc}")


def _version_ru(v: str) -> str:
    """«0.1.0 (git c3475ac-dirty)» становится «0.1.0 (git c3475ac, с незафиксированными изменениями)»."""
    return re.sub(r"(git [0-9a-f]{6,40})-dirty", lambda m: m.group(1) + ", с незафиксированными изменениями", v or "")


def _chart(title: str, head: str, rows: list[tuple[str, int]]) -> list[str]:
    """График как таблица с текстовой полосой: в Markdown читается как есть, в PDF рисуется столбчатая диаграмма."""
    rows = [(k, v) for k, v in rows if v]
    if not rows:
        return []
    top = max(v for _, v in rows)
    out = [f"<!-- chart: {title} -->", "", f"*{title}*", "", f"| {head} | Количество | |", "| :--- | ---: | :--- |"]
    out += [f"| {k} | {v} | {'█' * max(1, round(20 * v / top))} |" for k, v in rows]
    return out + ["", "<!-- /chart -->", ""]


def _plain(s: str) -> str:
    """Цитата без разметки, которую добавило чтение документа: жирный (**), ячейки таблиц (|).
    Слова и числа документа не меняются."""
    s = re.sub(r"\*\*|__", "", s or "")
    cells = [c.strip() for c in s.strip().strip("|").split("|")]
    return " / ".join(c for c in cells if c) if len(cells) > 1 else s.strip()


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


def write_extras(res: RunResult, report_path: str | Path, questions: bool = False) -> dict[str, Path]:
    """Файлы рядом с отчётом, только по запросу: форма вопросов с полями для ответов (questions=True,
    для повторной проверки с --answers) и исправленная редакция (если проверка шла с --fix).
    Сами вопросы всегда есть в отчёте, отдельный файл для них не обязателен."""
    from .clarify import write_questions

    rp = Path(report_path)
    out: dict[str, Path] = {}
    if questions and res.questions:
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
    return _save("\n".join(lines), p, src.name)


FORMATS = ("pdf", "md")


def report_paths(out: str | Path, fmt: str | None) -> list[Path]:
    """Пути отчёта по формату: pdf, md или both. Без формата он берётся из расширения (по умолчанию PDF)."""
    p = Path(out)
    ext = p.suffix.lower().lstrip(".")
    if fmt is None:
        fmt = ext if ext in FORMATS else "pdf"
    base = p.with_suffix("") if ext in FORMATS else p
    if fmt == "both":
        return [base.with_suffix(".pdf"), base.with_suffix(".md")]
    return [base.with_suffix("." + fmt)]


def _save(text: str, p: Path, doc_title: str = "") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix.lower() == ".pdf":
        from . import __version__
        from .pdf import footer_text, markdown_to_pdf

        return markdown_to_pdf(text, p, "Отчёт о проверке требований", doc_title, footer_text(__version__))
    p.write_text(text, encoding="utf-8")
    return p


def write(res: RunResult, out_path: str | Path) -> Path:
    """Отчёт в файл: .pdf свёрстан как документ, любое другое расширение получает Markdown."""
    return _save(Renderer(res).render(), Path(out_path), res.doc.title)
