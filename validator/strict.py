"""Строгая опора на документ: всё, что модель написала о документе, проверяется кодом.

Находки без цитаты отбрасываются ещё в конвейере (grounding.py). Здесь проверяется остальное, что попадает
в отчёт: ссылки на номера нарушений, требований и вопросов, номера строк, цитаты в вопросах, пункты «что
исправить в первую очередь», таблица сверки критериев приёмки. Неподтверждённое убирается, а не
переписывается: агент не дописывает за модель, он только отбрасывает то, чего нет в документе.

Функция идемпотентна: её можно вызвать после проверки и ещё раз при перерисовке сохранённой проверки.
"""

from __future__ import annotations

import re

from .grounding import Grounder

_REF = re.compile(r"\b(Н|ТР|В)-(\d{1,3})\b")
_LINE = re.compile(r"\b(строк[аеиу]?|строках)\s+(\d{1,5})(?:\s*(?:и|,)\s*(\d{1,5}))*", re.I)


def _grounder(res) -> Grounder:
    masked = None
    if res.audit_dir and (res.audit_dir / "masked_input.md").exists():
        masked = (res.audit_dir / "masked_input.md").read_text(encoding="utf-8")
    return Grounder(res.doc, masked)


def enforce(res) -> dict:
    """Убрать неподтверждённое из ответа модели. Возвращает и сохраняет в res.grounding счётчики."""
    g = _grounder(res)
    n_lines = len(res.doc.lines)
    fids = {f.fid for f in res.kept()}
    rids = {r.rid for r in res.requirements}
    nq = len(res.questions)
    stats = {"ids": 0, "lines": 0, "questions": 0, "quotes": 0, "fix_first": 0, "acceptance": 0}

    def clean(text: str) -> str:
        """Ссылки на несуществующие Н-/ТР-/В- и строки за пределами документа убираются из текста."""
        if not text:
            return text

        def ref(m):
            kind, num = m.group(1), int(m.group(2))
            ok = (kind == "Н" and m.group(0) in fids) or (kind == "ТР" and m.group(0) in rids) or \
                 (kind == "В" and 1 <= num <= nq)
            if ok:
                return m.group(0)
            stats["ids"] += 1
            return ""

        def line(m):
            nums = [int(x) for x in re.findall(r"\d+", m.group(0))]
            if all(1 <= x <= n_lines for x in nums):
                return m.group(0)
            stats["lines"] += 1
            return "в документе"

        text = _REF.sub(ref, text)
        text = _LINE.sub(line, text)
        # хвосты после удаления: «(, Н-03)», «()», двойные запятые и пробелы
        text = re.sub(r"\(\s*[,;\s]*\)", "", text)
        text = re.sub(r"\(\s*[,;]\s*", "(", text)
        text = re.sub(r"\s*[,;]\s*\)", ")", text)
        text = re.sub(r",\s*,", ",", text)
        return re.sub(r"[ \t]{2,}", " ", text).strip()

    # находки: текст модели
    for f in res.findings:
        f.problem, f.fix = clean(f.problem), clean(f.fix)

    # вопросы: связи с реальными находками и требованиями, дословная цитата
    kept_q = []
    for q in res.questions:
        q.finding_ids = [x for x in q.finding_ids if x in fids]
        q.req_ids = [x for x in q.req_ids if x in rids]
        if q.quote and not g.find(q.quote, min_score=85):
            q.quote = ""
            stats["quotes"] += 1
        if not q.finding_ids and not q.quote:
            stats["questions"] += 1  # вопрос ни к чему в документе не привязан
            continue
        q.question, q.why = clean(q.question), clean(q.why)
        q.options = [clean(o) for o in q.options]
        kept_q.append(q)
    res.questions = kept_q

    # итог: пункты «что исправить» только с реальными нарушениями
    c = res.conclusion
    if c:
        items = []
        for it in c.fix_first:
            it.finding_ids = [x for x in it.finding_ids if x in fids]
            if not it.finding_ids:
                stats["fix_first"] += 1
                continue
            it.title, it.action = clean(it.title), clean(it.action)
            items.append(it)
        c.fix_first = items
        c.development_rationale = clean(c.development_rationale)
        c.testing_rationale = clean(c.testing_rationale)
        c.overall = clean(c.overall)

    # набор: комментарии к оценкам
    res.set_verdicts = {k: (v, clean(cm)) for k, (v, cm) in res.set_verdicts.items()}

    # сверка критериев приёмки: «в примере» должно быть текстом документа. «По правилу» это вывод из правила
    # для условий примера (например, к какому месяцу относится покупка), он не цитата и не заменяется.
    for chk in getattr(res, "acceptance_checks", []) or []:
        ex = res.req(chk.acceptance_id)
        if ex and chk.stated and not g.find(chk.stated, min_score=85):
            chk.stated = ex.quote
            stats["acceptance"] += 1
        chk.expected, chk.question = clean(chk.expected), clean(chk.question)

    # покрытие: только существующие требования
    for row in getattr(res, "coverage", []) or []:
        row.acceptance_ids = [x for x in row.acceptance_ids if x in rids]
        row.missing_scenarios = [clean(s) for s in row.missing_scenarios]

    prev = getattr(res, "grounding", None) or {}
    stats = {k: stats[k] + prev.get(k, 0) for k in stats}
    stats["ungrounded_findings"] = len(res.ungrounded)
    stats["lost_requirements"] = len(res.lost_requirements)
    res.grounding = stats
    return stats
