"""Извлечение явно размеченных требований и примеров приёмки."""

from __future__ import annotations

import re
from collections import Counter

from .grounding import Grounder
from .schemas import AcceptanceCheck, Finding, Requirement

_FUNCTIONAL = re.compile(r"^([ФF][–—-]\d+)[.)]?\s+(.+)$", re.I)
_TABLE_SEPARATOR = re.compile(r"[\s:|-]+")
_MONTHS = ("январ", "феврал", "март", "апрел", "ма[йяе]", "июн", "июл",
           "август", "сентябр", "октябр", "ноябр", "декабр")
_DATE = re.compile(r"\b(?:[1-9]|[12]\d|3[01])\s+(" + "|".join(_MONTHS) + r")\w*\b", re.I)
_COUNTER_MONTH = re.compile(r"\b(" + "|".join(_MONTHS) + r")\w*\s+сч[её]тчик\b", re.I)


def _cells(text: str) -> list[str]:
    return [c.strip() for c in text.strip().strip("|").split("|")]


def extract(grounder: Grounder) -> tuple[list[Requirement], str]:
    """Возвращает найденные пункты и документ без их текста, сохраняя номера строк."""
    lines = grounder.src.split("\n")
    residual = lines.copy()
    found: list[Requirement] = []
    table_section = ""
    row_counts: Counter[str] = Counter()
    in_code = False
    for i, raw in enumerate(lines):
        section = grounder.doc.line_sections[i]
        line = raw.strip().replace("**", "")
        if line.startswith(("```", "~~~")):
            in_code = not in_code
            table_section = ""
        if in_code:
            continue
        title = section.rsplit(" > ", 1)[-1].casefold().replace("ё", "е")
        cells = _cells(line) if line.startswith("|") else []
        if title.startswith("критерии приемки") and [c.lower() for c in cells] == [
            "исходные условия", "действие", "ожидаемый результат",
        ]:
            table_section = section
            continue
        if _TABLE_SEPARATOR.fullmatch(line) and line.startswith("|"):
            continue
        if not line.startswith("|") or section != table_section:
            table_section = ""

        match = _FUNCTIONAL.fullmatch(line.lstrip("*- "))
        if title.startswith("функциональные требования") and match:
            # Пункт с продолжением обрабатывает модель, чтобы сохранить весь смысл.
            following = lines[i + 1].strip().replace("**", "") if i + 1 < len(lines) else ""
            if following and not following.startswith("#") and not _FUNCTIONAL.fullmatch(following.lstrip("*- ")):
                continue
            doc_id, body = match.groups()
            doc_id = re.sub("[–—]", "-", doc_id.upper())
            kind, summary = "functional", " ".join(body.split()[:15])
        elif table_section and len(cells) == 3 and all(cells) and not any(c == "—" for c in cells):
            row_counts[section] += 1
            doc_id = f"Критерий приёмки {row_counts[section]}"
            kind, summary = "acceptance", " ".join(cells[-1].split()[:15])
        else:
            continue
        anchor = grounder.line_anchor(i + 1)
        found.append(Requirement("", doc_id, section, summary, kind, anchor, anchor.text))
        residual[i] = ""
    return found, "\n".join(residual)


def purchase_month_checks(requirements: list[Requirement]) -> list[tuple[AcceptanceCheck, Finding]]:
    """Сверяет ожидаемый месяц в явно датированных сценариях покупки и проведения."""
    checks = []
    for rule in requirements:
        rule_text = rule.quote.lower()
        if rule.kind != "functional" or not re.search(
            r"\bмесяц определяется по дате совершения покупки в алматы\b", rule_text
        ):
            continue
        for example in requirements:
            cells = _cells(example.quote)
            if example.kind != "acceptance" or len(cells) != 3:
                continue
            before, action, outcome = cells
            if not before.lower().startswith("покупка ") or not action.lower().startswith("провести "):
                continue
            dates = _DATE.findall(before)
            posted = _DATE.findall(action)
            stated = _COUNTER_MONTH.search(outcome)
            if len(dates) != 1 or len(posted) != 1 or stated is None:
                continue
            expected_month = dates[0].lower()
            if expected_month == stated[1].lower() or posted[0].lower() != stated[1].lower():
                continue
            purchase_date = _DATE.search(before)[0]
            posting_date = _DATE.search(action)[0]
            question = "По какой дате определять месяц расходов: по дате совершения покупки или по дате её проведения?"
            expected = f"Учесть покупку в месяце её совершения ({purchase_date})."
            check = AcceptanceCheck(acceptance_id=example.rid, req_id=rule.rid, status="conflict",
                                    expected=expected, stated=outcome, question=question)
            finding = Finding("", "set", "set_consistent", "critical", [rule.rid, example.rid],
                              [rule.anchor, example.anchor],
                              f"По {rule.doc_id} покупка от {purchase_date} относится к месяцу совершения. "
                              f"Критерий приёмки учитывает её в месяце проведения ({posting_date}): {outcome}.",
                              question, "rule", verifier_note="Сопоставлены дата покупки, дата проведения и месяц счётчика.",
                              verification_status="confirmed")
            checks.append((check, finding))
    return checks
