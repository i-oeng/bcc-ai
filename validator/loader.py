"""Чтение документа требований: PDF, DOCX, Markdown, TXT, HTML.

Документ приводится к тексту в стиле Markdown (заголовки через '#'), чтобы
у каждой строки был номер, раздел и (для PDF) страница. По этим данным
отчёт ссылается на конкретное место в документе.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".txt", ".html", ".htm"}

# хотя бы одна формулировка требования на русском, казахском или английском
REQ_MARKERS = re.compile(
    r"должн|необходим|требовани|обязан|нужно|следует|может|могут|позволя|обеспечива|"
    r"shall|must|should|requir|тиіс|қажет|талап|мүмкін",
    re.IGNORECASE,
)
MIN_LETTERS = 15  # «Система должна работать.» уже можно проверить


class DocumentError(ValueError):
    """Документ нельзя проверить (пустой, повреждён, под паролем, без текста).

    Агент не падает, а пишет вместо отчёта понятную диагностику: что не так и что сделать.
    """

    def __init__(self, problem: str, advice: str = ""):
        self.problem = problem
        self.advice = advice
        super().__init__(f"{problem} {advice}".strip())


def _why(e: Exception) -> str:
    msg = " ".join(str(e).split())[:140]
    return f"техническая причина: {type(e).__name__}" + (f", {msg}" if msg else "")


@dataclass
class Document:
    path: Path
    fmt: str
    text: str
    sha256: str
    title: str = ""
    lines: list[str] = field(default_factory=list)
    line_pages: list[int | None] = field(default_factory=list)
    line_sections: list[str] = field(default_factory=list)

    def numbered(self) -> str:
        """Текст с номерами строк: так модель может ссылаться на место."""
        width = len(str(len(self.lines)))
        return "\n".join(f"{i + 1:>{width}}| {ln}" for i, ln in enumerate(self.lines))


def load(path: str | Path) -> Document:
    p = Path(path)
    if not p.exists():
        raise DocumentError(f"Файл не найден: {p}.", "Проверьте путь к документу.")
    if p.is_dir():
        raise DocumentError(f"Указана папка, а не файл: {p}.", "Передайте путь к файлу документа.")
    ext = p.suffix.lower()
    if ext not in SUPPORTED:
        raise DocumentError(
            f"Формат {ext or '(без расширения)'} не поддерживается.",
            f"Поддерживаются: {', '.join(sorted(SUPPORTED))}.",
        )
    raw = p.read_bytes()
    if not raw.strip():
        raise DocumentError(
            "Файл пустой (0 байт)." if not raw else "В файле только пробелы и переводы строк.",
            "Передайте документ с текстом требований.",
        )
    pages: list[int | None] | None = None
    if ext == ".pdf":
        text, pages = _read_pdf_checked(p, raw)
    elif ext == ".docx":
        text = _read_docx_checked(p)
    elif ext in {".html", ".htm"}:
        text = _read_html(_decode_text(raw))
    else:
        text = _decode_text(raw)
    text = _clean(text)
    letters = len(re.findall(r"[^\W\d_]", text))
    if letters == 0:
        raise DocumentError("В документе нет текста: после чтения не осталось ни одной буквы.",
                            "Передайте документ с текстом требований.")
    if letters < MIN_LETTERS:
        raise DocumentError(
            f"Текста слишком мало для проверки требований ({letters} букв).",
            "Похоже, документ обрезан или почти пуст. Передайте полный документ.",
        )
    if not REQ_MARKERS.search(text):
        raise DocumentError(
            "Это не похоже на документ требований: нет ни одной формулировки вида «система должна», "
            "«необходимо», «требование», shall или must.",
            "Проверьте, тот ли файл передан. Агент проверяет документы с требованиями (ТЗ, BRD, SRS, user stories).",
        )
    doc = Document(path=p, fmt=ext.lstrip("."), text=text, sha256=hashlib.sha256(raw).hexdigest())
    doc.lines = text.split("\n")
    if pages and 0 <= len(doc.lines) - len(pages) <= 2:
        doc.line_pages = pages + [pages[-1]] * (len(doc.lines) - len(pages))
    else:
        doc.line_pages = [None] * len(doc.lines)
    doc.line_sections = _sections(doc.lines)
    doc.title = _title(doc.lines, p)
    return doc


def _decode_text(raw: bytes) -> str:
    """Текстовый файл; двоичные данные под видом .txt/.md распознаются и не уходят в модель."""
    text = _decode(raw)
    sample = text[:65536]
    ctrl = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\t\n\r\f\v")
    bad = sample.count("\ufffd")
    if ctrl > len(sample) * 0.01 or bad > max(5, len(sample) * 0.01):
        raise DocumentError(
            "Файл не текстовый: внутри двоичные данные, а не текст.",
            "Возможно, у файла неверное расширение или он повреждён. Передайте документ в PDF, DOCX, MD или TXT.",
        )
    return text


def _read_pdf_checked(p: Path, raw: bytes) -> tuple[str, list[int | None]]:
    if b"%PDF" not in raw[:1024]:
        raise DocumentError(
            "Файл назван .pdf, но это не PDF: в начале нет сигнатуры %PDF.",
            "Файл повреждён или сохранён в другом формате. Откройте его и сохраните заново как PDF.",
        )
    import contextlib
    import io

    repaired = False
    # PyMuPDF запоминает поток для служебных сообщений при первом импорте: глушим, как в _read_pdf
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            import pymupdf
            import pymupdf4llm  # noqa: F401
        except ImportError:
            pymupdf = None
    try:
        if pymupdf is None:  # без PyMuPDF: проверку сделает pypdf в _read_pdf
            raise LookupError
        with pymupdf.open(str(p)) as d:
            if d.needs_pass:
                raise DocumentError("PDF защищён паролем: текст недоступен.",
                                    "Сохраните копию без пароля и передайте её.")
            if d.page_count == 0:
                raise DocumentError("PDF повреждён: в нём нет ни одной страницы.",
                                    "Сохраните документ заново или передайте его в DOCX/MD.")
            repaired = bool(getattr(d, "is_repaired", False))
    except DocumentError:
        raise
    except LookupError:
        pass
    except Exception as e:  # noqa: BLE001
        raise DocumentError(f"PDF повреждён и не открывается ({_why(e)}).",
                            "Откройте файл в программе просмотра и сохраните заново или передайте документ в DOCX/MD.") from e
    try:
        text, pages = _read_pdf(p)
    except Exception as e:  # noqa: BLE001
        raise DocumentError(f"Не удалось извлечь текст из PDF ({_why(e)}).",
                            "Сохраните документ заново или передайте его в DOCX/MD.") from e
    n_pages = max(1, max((x for x in pages if x), default=1))
    if len(re.sub(r"\s", "", text)) < 40 * n_pages:
        if repaired:
            raise DocumentError("PDF повреждён (обрезан или испорчен): текст прочитать не удалось.",
                                "Возьмите исходный файл заново или передайте документ в DOCX/MD.")
        raise DocumentError(
            f"В PDF почти нет текстового слоя ({n_pages} стр.): похоже на скан.",
            "Распознайте текст (OCR) или передайте документ в DOCX/MD.",
        )
    return text, pages


def _read_docx_checked(p: Path) -> str:
    import zipfile

    if not zipfile.is_zipfile(p):
        raise DocumentError(
            "Файл назван .docx, но не открывается как документ Word.",
            "Файл повреждён, защищён паролем или это другой формат (например, старый .doc). "
            "Сохраните документ в Word как .docx без пароля.",
        )
    with zipfile.ZipFile(p) as z:
        names = set(z.namelist())
    if "word/document.xml" not in names:
        raise DocumentError("Архив DOCX не содержит текста документа (нет word/document.xml).",
                            "Файл повреждён. Сохраните документ в Word заново.")
    try:
        return _read_docx(p)
    except Exception as e:  # noqa: BLE001
        raise DocumentError(f"DOCX повреждён и не читается ({_why(e)}).",
                            "Сохраните документ в Word заново или передайте его в PDF/MD.") from e


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    # PDF часто отдаёт дефис как мягкий перенос (U+00AD): «Ф­1» вместо «Ф-1»
    text = text.replace("­", "-")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n") + "\n"


def _read_pdf(p: Path) -> tuple[str, list[int | None]]:
    """PDF через PyMuPDF4LLM (Markdown с таблицами), запасной вариант pypdf."""
    chunks: list[tuple[int, str]] = []
    try:
        import contextlib
        import io

        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):  # служебные сообщения парсера
            import pymupdf4llm

            pages_md = pymupdf4llm.to_markdown(str(p), page_chunks=True, show_progress=False)
        for i, page in enumerate(pages_md):
            chunks.append((i + 1, page.get("text", "")))
    except Exception:
        from pypdf import PdfReader

        for i, page in enumerate(PdfReader(str(p)).pages):
            chunks.append((i + 1, page.extract_text() or ""))
    text_parts: list[str] = []
    pages: list[int | None] = []
    for num, chunk in chunks:
        chunk = _clean(chunk).rstrip("\n")
        if not chunk:
            continue
        for ln in chunk.split("\n"):
            text_parts.append(ln)
            pages.append(num)
    # _clean ещё раз схлопнет пустые строки, поэтому чистим здесь же и синхронно с pages
    out_lines: list[str] = []
    out_pages: list[int | None] = []
    blank = False
    for ln, pg in zip(text_parts, pages):
        # переносы строк внутри ячеек таблиц PyMuPDF4LLM отдаёт как <br>
        ln = ln.replace(" ", " ").replace("-<br>", "-").replace("<br>", " ").rstrip()
        if not ln:
            if blank:
                continue
            blank = True
        else:
            blank = False
        out_lines.append(ln)
        out_pages.append(pg)
    return "\n".join(out_lines), out_pages


def _read_docx(p: Path) -> str:
    """DOCX: абзацы и таблицы в исходном порядке; стили Heading N становятся '#'."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(p))
    out: list[str] = []
    for block in d.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            par = Paragraph(block, d)
            txt = par.text.strip()
            if not txt:
                out.append("")
                continue
            style = (par.style.name or "").lower() if par.style is not None else ""
            m = re.search(r"(heading|заголовок)\s*(\d)", style)
            if m:
                out.append("#" * int(m.group(2)) + " " + txt)
            elif style.startswith("title") or style.startswith("название"):
                out.append("# " + txt)
            elif "list" in style or "спис" in style:
                out.append("* " + txt)
            else:
                out.append(txt)
        elif tag == "tbl":
            table = Table(block, d)
            rows = []
            for row in table.rows:
                cells = []
                for c in row.cells:
                    cells.append(" ".join(c.text.split()))
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                ncols = rows[0].count("|") - 1
                rows.insert(1, "|" + " :--- |" * ncols)
            out.extend([""] + rows + [""])
    return "\n".join(out)


def _read_html(src: str) -> str:
    src = re.sub(r"(?is)<(script|style).*?</\1>", "", src)
    for n in range(1, 7):
        src = re.sub(rf"(?is)<h{n}[^>]*>(.*?)</h{n}>", lambda m, n=n: "\n" + "#" * n + " " + m.group(1) + "\n", src)
    src = re.sub(r"(?is)<li[^>]*>", "\n* ", src)
    src = re.sub(r"(?is)</t[dh]>", " | ", src)
    src = re.sub(r"(?is)<tr[^>]*>", "\n| ", src)
    src = re.sub(r"(?is)<br\s*/?>|</p>|</div>", "\n", src)
    src = re.sub(r"(?s)<[^>]+>", "", src)
    return html.unescape(src)


_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _sections(lines: list[str]) -> list[str]:
    """Путь раздела для каждой строки, например «Часть A > Функциональные требования»."""
    stack: list[tuple[int, str]] = []
    out: list[str] = []
    for ln in lines:
        m = _HEADING.match(ln)
        if m:
            level = len(m.group(1))
            title = m.group(2).replace("*", "").strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        # первый заголовок (название документа) в путь не включаем, если есть другие
        path = [t for lvl, t in stack if lvl > 1] or [t for _, t in stack]
        out.append(" > ".join(path))
    return out


def _title(lines: list[str], p: Path) -> str:
    for ln in lines:
        m = _HEADING.match(ln)
        if m:
            return m.group(2).replace("*", "").strip()
    for ln in lines:
        if ln.strip():
            return ln.strip()[:120]
    return p.stem
