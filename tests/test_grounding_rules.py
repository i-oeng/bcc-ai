from pathlib import Path

from validator import rules
from validator.grounding import Grounder
from validator.loader import load

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "requirements.md"


def test_load_markdown_sections():
    d = load(SAMPLE)
    assert d.title == "Уведомление о расходах по карте"
    i = next(i for i, ln in enumerate(d.lines) if "**Ф-3.**" in ln)
    assert "Функциональные требования" in d.line_sections[i]


def test_exact_and_fuzzy_quotes():
    d = load(SAMPLE)
    g = Grounder(d)
    a = g.find("Месяц определяется по дате совершения покупки в Алматы")
    assert a and a.score == 100 and "Ф-2" in d.lines[a.line - 1]
    # разметка, ё/е, кавычки и лишние пробелы не мешают
    b = g.find("Ф-1. Держатель активной карты KZT задает целый порог")
    assert b and b.line == a.line - 1
    # небольшие опечатки модели допускаются
    c = g.find("Сервис передаёт push на отправку в течении минуты после получения события")
    assert c and c.score >= 88
    # выдуманная цитата не находится
    assert g.find("Система должна поддерживать биометрическую аутентификацию по лицу") is None


def test_rules_find_template_defects():
    d = load(SAMPLE)
    hits = rules.run(d, d.text)
    kinds = {h.rule for h in hits}
    assert {"get_mutation", "units", "placeholder", "status_undocumented", "no_version", "compound", "empty_section"} <= kinds


def test_docx_reader(tmp_path):
    import docx

    doc = docx.Document()
    doc.add_heading("Тестовое ТЗ", level=1)
    doc.add_heading("Функциональные требования", level=2)
    doc.add_paragraph("Ф-1. Система должна отправлять push при достижении порога.")
    t = doc.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Поле", "Значение"
    t.cell(1, 0).text, t.cell(1, 1).text = "Порог", "1 000"
    p = tmp_path / "t.docx"
    doc.save(p)
    d = load(p)
    assert d.title == "Тестовое ТЗ"
    assert any("Ф-1. Система должна" in ln for ln in d.lines)
    assert any(ln.startswith("| Порог | 1 000 |") for ln in d.lines)


def test_pdf_table_quotes():
    d = load(SAMPLE.with_suffix(".pdf"))
    g = Grounder(d)
    # в PDF ячейки идут как |cardId|string|1–64|, модель цитирует их через пробел или «|»
    for q in ["cardId string 1–64", "cardId | string | 1–64 | Да", "Расходы 48 500 ₸, порог 50 000 ₸ Провести покупку на 1 500 ₸"]:
        a = g.find(q)
        assert a is not None and a.page is not None, q
