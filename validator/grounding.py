"""Привязка цитат к тексту документа.

Модель обязана цитировать документ дословно. Здесь каждая цитата ищется в
тексте (сначала точно после нормализации, затем нечётко). Найденная цитата
заменяется настоящим фрагментом документа и получает номер строки и раздел.
Ненайденная цитата считается выдуманной, а находка без единой опоры на текст
отбрасывается. Так в отчёт не попадают общие фразы без привязки к документу.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from .loader import Document

_TRANS = str.maketrans(
    {
        "ё": "е",
        "Ё": "Е",
        "«": '"',
        "»": '"',
        "“": '"',
        "”": '"',
        "„": '"',
        "’": "'",
        "‘": "'",
        "–": "-",
        "—": "-",
        "−": "-",
        "…": ".",
    }
)
_DROP = set("*`_#>")
_SPACE = set("|")


@dataclass
class Anchor:
    text: str  # фрагмент документа (как в оригинале)
    line: int  # номер первой строки, с 1
    line_end: int
    section: str
    page: int | None
    score: float  # 100 = точное совпадение

    def where(self) -> str:
        loc = f"строка {self.line}" if self.line == self.line_end else f"строки {self.line}-{self.line_end}"
        if self.page:
            loc = f"с. {self.page}, {loc}"
        if self.section:
            parts = self.section.split(" > ")
            sec = " > ".join([parts[0], parts[-1]]) if len(parts) > 2 else self.section
            loc += f", раздел «{sec}»"
        return loc


class Grounder:
    """Индекс документа для поиска цитат."""

    def __init__(self, doc: Document, text: str | None = None):
        # text: текст, который видела модель (после маскирования); по строкам совпадает с doc
        self.doc = doc
        self.src = text if text is not None else doc.text
        self.norm, self.idx = self._normalize(self.src)
        self.line_starts = [0]
        for i, ch in enumerate(self.src):
            if ch == "\n":
                self.line_starts.append(i + 1)

    @staticmethod
    def _normalize(s: str) -> tuple[str, list[int]]:
        """Нормализованная строка и отображение позиций обратно в исходник."""
        out: list[str] = []
        idx: list[int] = []
        prev_space = True
        for i, ch in enumerate(s):
            if ch in _DROP:
                continue
            ch = " " if ch in _SPACE else ch.translate(_TRANS).lower()
            if ch.isspace():
                if prev_space:
                    continue
                ch = " "
                prev_space = True
            else:
                prev_space = False
            out.append(ch)
            idx.append(i)
        return "".join(out), idx

    def _line_of(self, pos: int) -> int:
        lo, hi = 0, len(self.line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def _anchor(self, nstart: int, nend: int, score: float) -> Anchor:
        start = self.idx[nstart]
        end = self.idx[max(nstart, nend - 1)] + 1
        # нечёткое совпадение может начаться или закончиться посреди слова: расширяем до границ слов
        while start > 0 and self.src[start - 1].isalnum():
            start -= 1
        while end < len(self.src) and self.src[end].isalnum():
            end += 1
        l1, l2 = self._line_of(start), self._line_of(end - 1)
        return Anchor(
            text=self.src[start:end].strip(),
            line=l1,
            line_end=l2,
            section=self.doc.line_sections[l1 - 1] if l1 - 1 < len(self.doc.line_sections) else "",
            page=self.doc.line_pages[l1 - 1] if l1 - 1 < len(self.doc.line_pages) else None,
            score=score,
        )

    def find(self, quote: str, min_score: float = 88.0, _split: bool = True) -> Anchor | None:
        if not quote or not quote.strip():
            return None
        q, _ = self._normalize(quote.strip().strip('"').strip("«»"))
        q = q.strip(" .")
        if len(q) < 3:
            return None
        pos = self.norm.find(q)
        if pos >= 0:
            return self._anchor(pos, pos + len(q), 100.0)
        # модель могла склеить фрагменты через многоточие: ищем самый длинный кусок
        if "..." in quote or "…" in quote:
            parts = [x for x in re.split(r"\.\.\.|…", quote) if len(x.strip()) >= 8]
            best = None
            for part in sorted(parts, key=len, reverse=True):
                a = self.find(part, min_score, _split=False)
                if a and (best is None or a.score > best.score):
                    best = a
            if best:
                return best
        if len(q) < 12:
            return None
        al = fuzz.partial_ratio_alignment(q, self.norm, score_cutoff=min_score)
        if al is not None:
            return self._anchor(al.dest_start, al.dest_end, al.score)
        if not _split:
            return None
        # модель могла склеить цитату из соседних мест (строка статуса + пример ответа):
        # берём первый по порядку кусок размером с предложение, который есть в документе
        pieces = [p for p in re.split(r"(?<=[.;!?])\s+|\s*[{}]\s*", quote) if len(p.split()) >= 3 and len(p.strip()) >= 15]
        if not pieces or (len(pieces) == 1 and pieces[0].strip() == quote.strip()):
            return None
        for piece in pieces:
            a = self.find(piece, min_score, _split=False)
            if a:
                a.score = min(a.score, 90.0)  # частичная опора
                return a
        return None

    def line_anchor(self, line: int) -> Anchor:
        """Опора на целую строку (для правил, которые работают построчно)."""
        line = max(1, min(line, len(self.doc.lines)))
        text = self.src.split("\n")[line - 1].strip().lstrip("*-#> ").strip()
        return Anchor(
            text=text,
            line=line,
            line_end=line,
            section=self.doc.line_sections[line - 1],
            page=self.doc.line_pages[line - 1],
            score=100.0,
        )
