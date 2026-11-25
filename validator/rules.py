"""Детерминированные проверки без LLM.

Ловят то, что надёжнее найти правилом, чем моделью: размытые слова, пустые
поля шаблона, отсутствие версии и приоритетов, смешение денежных единиц,
изменяющие операции через GET, коды ответа без описания. Находки правил
проходят тот же верификатор, что и находки модели.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .loader import Document


@dataclass
class RuleHit:
    rule: str
    criterion: str
    severity: str
    lines: list[int]
    problem: str
    fix: str
    whole_doc: bool = False  # относится к документу целиком


# слово (регэксп) -> почему плохо
VAGUE = {
    r"быстр\w*": "«быстро» не измеримо: нужен порог времени и точка измерения",
    r"оперативн\w*": "«оперативно» не измеримо: нужен порог времени",
    r"своевременн\w*": "«своевременно» не измеримо",
    r"удобн\w*": "«удобно» субъективно и не проверяемо",
    r"интуитивн\w*": "«интуитивно» субъективно и не проверяемо",
    r"понятн\w*": "«понятно» субъективно",
    r"при необходимости": "не сказано, кто и по какому признаку определяет необходимость",
    r"по возможности": "делает требование необязательным",
    r"по мере возможности": "делает требование необязательным",
    r"и т\.\s?д\.?|и т\.\s?п\.?|и так далее|и прочее|и др\.": "открытый перечень: неизвестно, что ещё входит",
    r"и далее": "открытый перечень: неизвестно, какие ещё значения входят",
    r"как правило|обычно|в основном": "допускает исключения, которые не описаны",
    r"достаточн\w*": "«достаточно» без меры",
    r"максимальн\w* (?:быстр|удобн|эффективн)\w*": "превосходная степень без меры",
    r"эффективн\w*": "«эффективно» без меры",
    r"надёжн\w*|надежн\w*": "«надёжно» без метрики доступности/ошибок",
    r"в момент\b": "«в момент» неизмеримо: нужна допустимая задержка и точка отсчёта",
    r"мгновенн\w*|сразу\b|немедленн\w*": "«мгновенно» неизмеримо: нужна допустимая задержка",
    r"некотор\w*|различн\w*|соответствующ\w*": "неопределённый объём",
    r"и/или": "неясно, нужно одно, другое или оба",
    r"приблизительно|около\b|порядка\b": "неточная величина",
    r"и тому подобное": "открытый перечень",
}

PLACEHOLDERS = [
    (r"\bTBD\b|\bTODO\b|\bуточнить\b|\bуточняется\b", "В документе остались пометки «уточнить/TBD»"),
    (r"не заполнен\w*|заполняются\s+\w+|будет заполнен\w*|будут заполнены", "Разделы документа не заполнены"),
    (r"Да\s*/\s*Нет", "В чек-листе ревью не выбран вариант «Да / Нет», ревью не проведено"),
]

MODAL = re.compile(r"\b(должен|должна|должно|должны|обязан\w*|необходимо|требуется|shall|must)\b", re.I)
REQ_ID_LINE = re.compile(r"^\s*[*\-]?\s*\**\s*([A-ZА-ЯЁ]{1,4}[-\s]?\d+(?:\.\d+)*)\.?\**\s+(.+)$")
MUTATING_URL = re.compile(r"/[\w/]*(save|create|update|delete|remove|set|add|edit|change|cancel)\w*", re.I)
MUTATING_DESC = re.compile(r"сохранени|изменени|создани|удалени|обновлени|отмен", re.I)


def run(doc: Document, text: str) -> list[RuleHit]:
    lines = text.split("\n")
    hits: list[RuleHit] = []
    hits += _vague(lines)
    hits += _placeholders(lines)
    hits += _empty_sections(lines, doc)
    hits += _singular_and_modal(lines)
    hits += _units(lines)
    hits += _http(lines, doc)
    hits += _status_codes(lines, doc)
    hits += _doc_level(lines)
    return hits


def _vague(lines: list[str]) -> list[RuleHit]:
    out = []
    for i, ln in enumerate(lines, 1):
        if ln.lstrip().startswith("#") or ln.lstrip().startswith("```"):
            continue
        for rx, why in VAGUE.items():
            m = re.search(rf"(?<![\w-])(?:{rx})", ln, re.I)
            if m:
                out.append(
                    RuleHit(
                        "vague_term", "unambiguous", "major", [i],
                        f"Размытая формулировка «{m.group(0)}»: {why}.",
                        "Заменить на измеримое условие (число, единица, граница, точка отсчёта) или закрытый перечень.",
                    )
                )
    return out


def _placeholders(lines: list[str]) -> list[RuleHit]:
    out = []
    groups: dict[str, list[int]] = {}
    for i, ln in enumerate(lines, 1):
        for rx, why in PLACEHOLDERS:
            if re.search(rx, ln, re.I):
                groups.setdefault(why, []).append(i)
    for why, ls in groups.items():
        sev = "major" if "Да / Нет" in why else "critical" if "не заполнен" in why else "major"
        crit = "set_traceable" if "Да / Нет" in why else "set_complete"
        out.append(
            RuleHit(
                "placeholder", crit, sev, ls[:6],
                f"{why} (мест: {len(ls)}; строки {', '.join(map(str, ls[:12]))}{'…' if len(ls) > 12 else ''}).",
                "Заполнить раздел до передачи в разработку или явно указать «не требуется» с обоснованием.",
                whole_doc=True,
            )
        )
    # «—» как значение поля в таблице параметров
    dash_cells = []
    for i, ln in enumerate(lines, 1):
        if ln.startswith("|") and ln.count("|") == 3 and re.search(r"\|\s*—\s*\||:\s*—\s*\|", ln):
            dash_cells.append(i)
    if dash_cells:
        out.append(
            RuleHit(
                "dash_value", "set_complete", "major", dash_cells,
                f"Поля таблицы оставлены пустыми («—»), строки {', '.join(map(str, dash_cells))}.",
                "Указать значение или явно написать «не применяется» с причиной.",
            )
        )
    return out


_HEAD = re.compile(r"^(#{1,6})\s+(.*\S)")
_SEP = re.compile(r"^\|?\s*:?-{3,}")


def _empty_sections(lines: list[str], doc: Document) -> list[RuleHit]:
    """Раздел без данных: только шапка таблицы, пустые строки таблицы или жирные подписи.

    В Markdown пустая таблица видна как строки «| | |»; в PDF она схлопывается
    в одну строку с заголовками столбцов. Оба случая ловятся одинаково.
    """
    heads = [(i, len(m.group(1)), m.group(2).replace("*", "").strip())
             for i, ln in enumerate(lines, 1) if (m := _HEAD.match(ln))]
    out = []
    for k, (i, level, title) in enumerate(heads):
        nxt = heads[k + 1] if k + 1 < len(heads) else None
        if nxt and nxt[1] > level and nxt[0] - i <= 2:
            continue  # родительский раздел, содержимое в подразделах
        stop = nxt[0] if nxt else len(lines) + 1
        body = [(j, lines[j - 1].strip()) for j in range(i + 1, stop)
                if lines[j - 1].strip() and not re.fullmatch(r"[-*_]{3,}", lines[j - 1].strip())]
        if not body or any(t.startswith("```") for _, t in body):
            continue
        has_frame = False
        has_data = False
        table_row = 0
        for _, t in body:
            if t.startswith("|"):
                table_row += 1
                if _SEP.match(t):
                    continue
                cells = [c.strip() for c in t.strip("|").split("|")]
                if table_row == 1 or all(not c or c.startswith("**") and c.endswith("**") for c in cells):
                    has_frame = True  # шапка таблицы
                elif any(cells):
                    has_data = True
                else:
                    has_frame = True  # пустая строка таблицы
            elif re.fullmatch(r"\*\*[^*]+\*\*", t):
                has_frame = True  # только жирная подпись (шапка таблицы в PDF)
                table_row = 0
            else:
                has_data = True
                table_row = 0
        if has_frame and not has_data:
            out.append(
                RuleHit(
                    "empty_section", "set_complete", "major", [body[0][0]],
                    f"Раздел «{title}» не заполнен: есть только заголовки таблицы, данных нет.",
                    "Заполнить раздел (для блока согласования: подразделение, ФИО, подпись, дата) или указать, почему он не нужен.",
                    whole_doc=True,
                )
            )
    return out


def _singular_and_modal(lines: list[str]) -> list[RuleHit]:
    out = []
    no_modal: list[tuple[int, str]] = []
    for i, ln in enumerate(lines, 1):
        m = REQ_ID_LINE.match(ln)
        if not m:
            continue
        rid, body = m.group(1), m.group(2)
        body_clean = body.replace("*", "")
        sentences = [s for s in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z])", body_clean.strip()) if len(s) > 3]
        if len(sentences) >= 2:
            out.append(
                RuleHit(
                    "compound", "singular", "minor", [i],
                    f"Требование {rid} содержит {len(sentences)} самостоятельных утверждения: их нельзя "
                    "проверить и проследить по отдельности.",
                    f"Разбить {rid} на атомарные требования ({rid}.1, {rid}.2, …), у каждого свой критерий приёмки.",
                )
            )
        if not MODAL.search(body):
            no_modal.append((i, rid))
    if len(no_modal) >= 2:
        ids = ", ".join(r for _, r in no_modal)
        out.append(
            RuleHit(
                "modality", "conforming", "minor", [i for i, _ in no_modal],
                f"Требования {ids} записаны в изъявительном наклонении («задаёт», «получает») без модального глагола: "
                "непонятно, это обязательство системы, описание текущего поведения или пожелание.",
                "Использовать единую модальность: «Система должна …» для обязательных требований.",
                whole_doc=True,
            )
        )
    return out


def _units(lines: list[str]) -> list[RuleHit]:
    out = []
    for i, ln in enumerate(lines, 1):
        low = ln.lower()
        if re.search(r"тиын", low) and re.search(r"тенге|₸|kzt", low):
            out.append(
                RuleHit(
                    "units", "set_consistent", "major", [i],
                    "В одной строке денежные величины хранятся в разных единицах (тиыны и тенге): при сравнении "
                    "суммы с порогом возможна ошибка в 100 раз.",
                    "Хранить все суммы в одной минимальной единице (тиыны) и явно указать правило перевода.",
                )
            )
    return out


def _http(lines: list[str], doc: Document) -> list[RuleHit]:
    out = []
    method_line = None
    for i, ln in enumerate(lines, 1):
        if re.search(r"rest\s*type|http\s*method|метод\s*http", ln, re.I) and re.search(r"\bGET\b", ln):
            method_line = i
    if method_line:
        mutating = [i for i, ln in enumerate(lines, 1) if MUTATING_URL.search(ln)]
        desc = [i for i, ln in enumerate(lines, 1) if re.search(r"description|описание", ln, re.I) and MUTATING_DESC.search(ln)]
        if mutating or desc:
            out.append(
                RuleHit(
                    "get_mutation", "appropriate", "major", [method_line] + (mutating[:1] or desc[:1]),
                    "Изменяющая операция (сохранение настройки) объявлена как GET. GET по HTTP-семантике безопасный и "
                    "кэшируемый: прокси и браузер могут повторить или закэшировать запрос, параметры попадают в логи и историю.",
                    "Использовать PUT (замена настройки) или POST, параметры передавать в теле JSON.",
                )
            )
    body_ct = [i for i, ln in enumerate(lines, 1) if re.search(r"content-type", ln, re.I)]
    no_body = [i for i, ln in enumerate(lines, 1) if re.search(r"тело не используется|без тела|body is not used", ln, re.I)]
    if body_ct and no_body:
        out.append(
            RuleHit(
                "content_type_no_body", "set_consistent", "minor", [body_ct[0], no_body[0]],
                "Для запроса указан Content-Type, но сказано, что тело не используется.",
                "Либо убрать Content-Type, либо описать тело запроса.",
            )
        )
    return out


def _status_codes(lines: list[str], doc: Document) -> list[RuleHit]:
    defined: dict[str, int] = {}
    examples: dict[str, int] = {}
    mentioned: dict[str, int] = {}
    for i, ln in enumerate(lines, 1):
        sec = (doc.line_sections[i - 1] if i - 1 < len(doc.line_sections) else "").lower()
        if not re.search(r"ответ|статус|response|status|код", sec + " " + ln.lower()):
            continue
        for c in re.findall(r"\*\*([1-5]\d\d)\.\*\*", ln):
            defined.setdefault(c, i)
        m = re.match(r"^\s*#+\s*\**\s*([1-5]\d\d)\b", ln) or re.match(r"^\s*\*\*([1-5]\d\d)\*\*\s*$", ln)
        if m:
            examples.setdefault(m.group(1), i)
        if not ln.lstrip().startswith("{"):
            for c in re.findall(r"(?<![\d.\w])([1-5]\d\d)(?![\d\w])", ln):
                mentioned.setdefault(c, i)
    if not defined:
        return []
    out = []
    undocumented = sorted((set(mentioned) | set(examples)) - set(defined))
    undocumented = [c for c in undocumented if c in examples or re.search(r"\b" + c + r"\b", lines[mentioned[c] - 1])]
    for c in undocumented:
        line = examples.get(c) or mentioned[c]
        out.append(
            RuleHit(
                "status_undocumented", "set_complete", "major", [line],
                f"Код ответа {c} упоминается в описании, но отсутствует в перечне статусов метода "
                f"(перечень: {', '.join(sorted(defined))}).",
                f"Добавить {c} в перечень статусов с условием возникновения и примером ответа либо убрать упоминание.",
            )
        )
    no_example = sorted(set(defined) - set(examples))
    for c in no_example:
        out.append(
            RuleHit(
                "status_no_example", "set_complete", "minor", [defined[c]],
                f"Для кода {c} нет отдельного примера ответа.",
                f"Добавить пример тела ответа {c}.",
            )
        )
    return out


def checklist_items(lines: list[str]) -> list[str]:
    """Пункты чек-листа ревью: строки таблицы, где варианты ответа «Да / Нет» не выбраны."""
    items = []
    for ln in lines:
        if ln.lstrip().startswith("|") and re.search(r"Да\s*/\s*Нет", ln):
            first = ln.strip().strip("|").split("|")[0].replace("*", "").strip()
            if first and not re.fullmatch(r"Да\s*/\s*Нет.*", first) and first not in items:
                items.append(first)
    return items


def _doc_level(lines: list[str]) -> list[RuleHit]:
    text = "\n".join(lines)
    out = []
    if not re.search(r"верси[яи]\s*(?:документа)?\s*[:№]?\s*v?\d", text, re.I):
        ln = next((i for i, l in enumerate(lines, 1) if re.search(r"верси", l, re.I)), 1)
        out.append(
            RuleHit(
                "no_version", "set_traceable", "major", [ln],
                "У документа нет номера версии и даты: нельзя понять, какую редакцию проверяли и реализуют.",
                "Добавить лист изменений: версия, дата, автор, что изменено.",
                whole_doc=True,
            )
        )
    if not re.search(r"приоритет|priority|moscow|\bmust\b|\bshould\b|обязательн\w* к реализации", text, re.I):
        out.append(
            RuleHit(
                "no_priority", "set_ranked", "minor", [1],
                "Ни у одного требования нет приоритета или признака обязательности/стабильности.",
                "Указать приоритет (например, MoSCoW) и стабильность для каждого требования.",
                whole_doc=True,
            )
        )
    return out
