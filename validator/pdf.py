"""Отчёт в PDF: тот же Markdown, свёрстанный как официальный документ формата A4.

Вёрстка через PyMuPDF (уже в зависимостях): Story раскладывает HTML по страницам, шрифты встроены в
MuPDF (Charis SIL с кириллицей и Nimbus Sans), поэтому на сервере ничего доустанавливать не нужно.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

# Только чёрный: акценты делаются жирным, курсивом и прописными, а не цветом.
INK = "#000000"

CSS = f"""
body {{ font-family: serif; font-size: 10pt; line-height: 1.4; color: {INK}; }}
h1 {{ font-size: 19pt; font-weight: bold; margin: 0 0 3pt 0; }}
h1 + p {{ font-size: 12pt; margin: 0 0 10pt 0; }}
h2 {{ font-size: 12.5pt; font-weight: bold; margin: 16pt 0 6pt 0; padding-bottom: 2pt;
      border-bottom: 0.75pt solid {INK}; }}
h3 {{ font-size: 10.5pt; font-weight: bold; font-style: italic; margin: 11pt 0 4pt 0; }}
p {{ margin: 0 0 5pt 0; }}
ul, ol {{ margin: 0 0 5pt 0; }}
li {{ margin: 0 0 3pt 0; }}
code {{ font-family: monospace; font-size: 8.5pt; }}
blockquote {{ margin: 4pt 0 10pt 0; padding: 7pt 9pt; border: 0.75pt solid {INK}; }}
blockquote p {{ margin: 0; }}
table {{ border-collapse: collapse; width: 100%; margin: 2pt 0 9pt 0; border-top: 0.9pt solid {INK}; }}
th {{ font-family: sans-serif; font-size: 7.5pt; font-weight: bold; text-align: left;
      border-bottom: 0.6pt solid {INK}; padding: 3pt 4pt 3pt 0; }}
td {{ font-family: sans-serif; font-size: 8pt; line-height: 1.3; border-bottom: 0.25pt solid {INK};
      padding: 3.5pt 5pt 3.5pt 0; vertical-align: top; }}
td em {{ font-family: serif; font-size: 8.5pt; }}
table.kv td {{ padding-top: 2.5pt; padding-bottom: 2.5pt; }}
table.matrix td, table.matrix th {{ padding: 2pt 3pt 2pt 0; }}
p.legend {{ font-size: 8pt; font-style: italic; }}
"""

MARGINS = (62, 64, 46, 58)  # левое, верхнее, правое, нижнее поле в пунктах (около 22, 23, 16, 20 мм)


def _html(md: str) -> str:
    try:
        import markdown
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("Для PDF нужен пакет markdown: pip install -r requirements.txt") from e
    html = markdown.markdown(md, extensions=["tables", "sane_lists"])
    # выравнивание влево и так по умолчанию; без атрибута ниже срабатывают ширины колонок
    html = html.replace(' style="text-align: left;"', "")
    # таблицы «параметр / значение» и матрица критериев
    html = html.replace("<table>\n<thead>\n<tr>\n<th>Параметр</th>",
                        '<table class="kv">\n<thead>\n<tr>\n<th width="20%">Параметр</th>')
    html = html.replace("<th>ID в отчёте</th>\n<th>ID в документе</th>\n<th>Краткая формулировка</th>\n"
                        "<th>Цитата из документа</th>\n<th>Строка</th>",
                        '<th width="9%">ID в отчёте</th>\n<th width="17%">ID в документе</th>\n'
                        '<th width="30%">Краткая формулировка</th>\n<th width="34%">Цитата из документа</th>\n'
                        '<th width="10%">Строка</th>')
    html = html.replace("<th>Показатель</th>", '<th width="45%">Показатель</th>')
    html = html.replace("<th>Критерий</th>\n<th>Оценка</th>\n<th>Комментарий</th>",
                        '<th width="22%">Критерий</th>\n<th width="13%">Оценка</th>\n<th width="65%">Комментарий</th>')
    html = html.replace("<table>\n<thead>\n<tr>\n<th>ТР</th>\n<th>В документе</th>",
                        '<table class="matrix">\n<thead>\n<tr>\n<th width="8%">ТР</th>\n<th width="24%">В документе</th>')
    html = html.replace("<p>× нарушение", '<p class="legend">× нарушение')
    # ширины колонок таблицы нарушений
    html = html.replace(
        "<th>№</th>\n<th>Где</th>\n<th>Критерий</th>\n<th>Нарушение и цитата</th>\n<th>Как исправить</th>",
        '<th width="10%">№</th>\n<th width="15%">Где</th>\n<th width="13%">Критерий</th>\n'
        '<th width="36%">Нарушение и цитата</th>\n<th width="26%">Как исправить</th>')
    return html


_CHART = re.compile(r"<!-- chart: (.*?) -->(.*?)<!-- /chart -->", re.S)
_CELL = re.compile(r"<tr>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(\d+)</td>", re.S)


CHART_W, LABEL_W, ROW_H, HEAD_H = 470, 190, 18, 22


def _charts(html: str) -> tuple[str, dict[str, tuple[str, list[tuple[str, int]]]], dict[str, bytes]]:
    """Таблицы-графики из отчёта: в вёрстке остаётся пустое место точного размера, сам график потом
    рисуется векторно (линии и текст PDF), поэтому он чёткий при любом увеличении и при печати."""
    charts: dict[str, tuple[str, list[tuple[str, int]]]] = {}
    files: dict[str, bytes] = {}

    def place(m):
        title, body = m.group(1), m.group(2)
        rows = [(re.sub(r"<[^>]+>", "", k).strip(), int(v)) for k, v in _CELL.findall(body)]
        if not rows:
            return m.group(0)
        cid = f"chart{len(charts) + 1}"
        charts[cid] = (title, rows)
        h = ROW_H * len(rows) + 8 + HEAD_H
        files[f"{cid}.svg"] = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{CHART_W}" height="{h}"></svg>').encode()
        return f'<p id="{cid}"><img src="{cid}.svg" width="{CHART_W}" height="{h}"/></p>'

    return _CHART.sub(place, html), charts, files


def _draw_chart(page, rect, title: str, rows: list[tuple[str, int]]) -> None:
    """Столбчатая диаграмма прямо на странице: чёрные полосы, подписи и числа текстом."""
    import pymupdf

    x0, y0 = rect.x0, rect.y0
    serif = "p {font-family: serif; font-size: 10pt; font-style: italic; margin: 0; color: #000;}"
    sans = "p {font-family: sans-serif; font-size: 8.5pt; margin: 0; color: #000;}"
    page.insert_htmlbox(pymupdf.Rect(x0, y0, x0 + CHART_W, y0 + HEAD_H), f"<p>{_esc(title)}</p>", css=serif)
    top = max(v for _, v in rows) or 1
    bar_max = CHART_W - LABEL_W - 40
    for i, (label, v) in enumerate(rows):
        y = y0 + HEAD_H + 4 + i * ROW_H
        bw = max(1.5, bar_max * v / top)
        page.insert_htmlbox(pymupdf.Rect(x0, y + 2, x0 + LABEL_W - 8, y + ROW_H), f"<p>{_esc(label)}</p>", css=sans)
        page.draw_rect(pymupdf.Rect(x0 + LABEL_W, y + 3, x0 + LABEL_W + bw, y + 14), color=None, fill=(0, 0, 0))
        page.insert_htmlbox(pymupdf.Rect(x0 + LABEL_W + bw + 5, y + 2, x0 + CHART_W, y + ROW_H),
                            f"<p><b>{v}</b></p>", css=sans)
    page.draw_line((x0 + LABEL_W, y0 + HEAD_H + 2), (x0 + LABEL_W, y0 + HEAD_H + 6 + ROW_H * len(rows)),
                   color=(0, 0, 0), width=0.6)


def markdown_to_pdf(md: str, out: str | Path, title: str, doc_title: str = "", footer: str = "") -> Path:
    import pymupdf

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    html, charts, files = _charts(_html(md))
    archive = pymupdf.Archive()
    for name, data in files.items():
        archive.add(data, name)
    story = pymupdf.Story(html=f"<body>{html}</body>", user_css=CSS, archive=archive)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    page = pymupdf.paper_rect("a4")
    left, top, right, bottom = MARGINS
    where = pymupdf.Rect(left, top, page.width - right, page.height - bottom)
    spots: dict[str, tuple[int, tuple]] = {}
    pno = 0

    def seen(pos):
        if getattr(pos, "id", "") in charts and pos.open_close & 1:
            spots.setdefault(pos.id, (pno, tuple(pos.rect)))

    more = 1
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.element_positions(seen)
        story.draw(dev)
        writer.end_page()
        pno += 1
    writer.close()

    doc = pymupdf.open("pdf", buf.getvalue())
    for cid, (p_i, r) in spots.items():
        _draw_chart(doc[p_i], pymupdf.Rect(r), *charts[cid])
    n = doc.page_count
    small = "font-family: sans-serif; font-size: 7pt; color: #000;"
    for i, pg in enumerate(doc, 1):
        w, h = pg.rect.width, pg.rect.height
        if i > 1:  # колонтитул: на первой странице заголовок уже есть
            head = f"{title}" + (f" / {_esc(doc_title)}" if doc_title else "")
            pg.insert_htmlbox(pymupdf.Rect(left, 30, w - right, 44), f"<p>{head}</p>", css=f"p {{{small} margin:0}}")
            pg.draw_line((left, 46), (w - right, 46), color=(0, 0, 0), width=0.4)
        else:
            pg.draw_line((left, 40), (w - right, 40), color=(0, 0, 0), width=1.2)
        pg.draw_line((left, h - 40), (w - right, h - 40), color=(0, 0, 0), width=0.4)
        pg.insert_htmlbox(pymupdf.Rect(left, h - 37, w - right - 90, h - 22), f"<p>{_esc(footer)}</p>",
                          css=f"p {{{small} margin:0}}")
        pg.insert_htmlbox(pymupdf.Rect(w - right - 90, h - 37, w - right, h - 22),
                          f"<p>Страница {i} из {n}</p>", css=f"p {{{small} margin:0; text-align:right}}")
    doc.set_metadata({"title": f"{title}: {doc_title}" if doc_title else title, "author": "oresa",
                      "creator": "oresa", "producer": "PyMuPDF",
                      "creationDate": pymupdf.get_pdf_now(), "modDate": pymupdf.get_pdf_now()})
    try:  # подписи графиков встраивают шрифт заново: оставляем только нужные символы, файл в разы меньше
        doc.subset_fonts()
    except Exception:  # noqa: BLE001
        pass
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()
    return out


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _rgb(hex_: str) -> tuple[float, float, float]:
    h = hex_.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def footer_text(version: str) -> str:
    return f"Сформировано агентом oresa {version}, {datetime.now():%d.%m.%Y %H:%M}"
