"""Цикл уточнения: файл вопросов для заказчика и разбор его ответов.

После проверки агент пишет рядом с отчётом файл <отчёт>_questions.md. Аналитик
передаёт его заказчику, тот вписывает ответы после «Ответ:» (можно номер
варианта). Повторная проверка с --answers учитывает ответы: закрывает дефекты,
которые они устраняют, и пересчитывает готовность документа. Шаги, не
зависящие от ответов, берутся из кэша, поэтому повтор стоит 2 запроса.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

PRIORITY = {"blocking": "блокирующий", "important": "важный", "nice": "желательный"}


@dataclass
class QA:
    qid: str
    question: str
    answer: str
    options: list[str] = field(default_factory=list)
    finding_ids: list[str] = field(default_factory=list)


def write_questions(res, path: str | Path) -> Path:
    from .report import Renderer

    r = Renderer(res)
    p = Path(path)
    lines = [
        f"# Вопросы для уточнения: {r.cell(res.doc.title, model=False)}",
        "",
        f"Документ: `{res.doc.path.name}` (SHA-256 `{res.doc.sha256[:16]}…`). Сформировано агентом "
        f"{res.started.strftime('%d.%m.%Y %H:%M')}.",
        "",
        "Впишите ответ после «Ответ:» (текстом или номером варианта). Пустой ответ означает, что вопрос остаётся открытым. "
        "Затем запустите повторную проверку:",
        "",
        "```bash",
        f"python -m validator {res.doc.path.as_posix()} <отчёт.md> --answers {p.as_posix()}",
        "```",
    ]
    for i, q in enumerate(res.questions, 1):
        lines += ["", f"## В-{i}. {r.t(q.question)}", ""]
        lines.append(f"- Приоритет: {PRIORITY.get(q.priority, q.priority)}; кому: {r.cell(q.addressee)}")
        if q.quote:
            lines.append(f"- Контекст: «{r.cell(q.quote, 300, model=False)}»")
        lines.append(f"- Почему важно: {r.t(q.why)}")
        for j, o in enumerate(q.options, 1):
            lines.append(f"- Вариант {j}: {r.t(o)}")
        ids = [x for x in q.finding_ids if x]
        if ids:
            lines.append(f"- Связанные находки: {', '.join(ids)}")
        lines += ["", "Ответ: "]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


_Q = re.compile(r"^##\s+(В-\d+)\.\s+(.*\S)\s*$")


def parse_answers(path: str | Path) -> list[QA]:
    text = Path(path).read_text(encoding="utf-8-sig")
    out: list[QA] = []
    cur: QA | None = None
    in_answer = False
    buf: list[str] = []

    def flush():
        nonlocal cur, buf
        if cur is not None:
            ans = " ".join(x.strip() for x in buf if x.strip()).strip()
            m = re.fullmatch(r"\(?(\d)\)?\.?", ans)
            if m and cur.options and 1 <= int(m.group(1)) <= len(cur.options):
                ans = f"Вариант {m.group(1)}: {cur.options[int(m.group(1)) - 1]}"
            cur.answer = ans
            out.append(cur)
        cur, buf = None, []

    for ln in text.splitlines():
        m = _Q.match(ln)
        if m:
            flush()
            cur = QA(qid=m.group(1), question=m.group(2), answer="")
            in_answer = False
            continue
        if cur is None:
            continue
        mo = re.match(r"^-\s*Вариант\s+\d+:\s*(.*)$", ln)
        if mo and not in_answer:
            cur.options.append(mo.group(1).strip())
            continue
        mf = re.match(r"^-\s*Связанные находки:\s*(.*)$", ln)
        if mf and not in_answer:
            cur.finding_ids = [x.strip() for x in mf.group(1).split(",") if x.strip()]
            continue
        ma = re.match(r"^\s*Ответ:\s*(.*)$", ln)
        if ma:
            in_answer = True
            buf.append(ma.group(1))
            continue
        if in_answer:
            buf.append(ln)
    flush()
    return [q for q in out if q.answer]


def qa_block(answers: list[QA]) -> str:
    if not answers:
        return "(ответов пока нет)"
    out = []
    for q in answers:
        rel = f" (связанные находки: {', '.join(q.finding_ids)})" if q.finding_ids else ""
        out.append(f"{q.qid}. Вопрос: {q.question}{rel}\n   Ответ: {q.answer}")
    return "\n".join(out)
