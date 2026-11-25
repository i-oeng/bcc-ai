"""Маскирование чувствительных данных перед отправкой в облачную LLM.

Как работает:
1. Детекторы ищут в тексте номера карт (с проверкой Луна), ИИН/БИН (с
   контрольной суммой), IBAN (mod 97), телефоны, e-mail, IP-адреса, URL с
   хостом, токены и ключи, ФИО (Natasha NER, офлайн) и термины из списка
   заказчика.
2. Каждое значение заменяется плейсхолдером вида [КАРТА_1]. Одно и то же
   значение всегда получает один и тот же плейсхолдер, поэтому модель видит,
   что речь об одной карте.
3. Таблица соответствий остаётся только на этой машине. В отчёте исходные
   значения возвращаются на место.
4. Перед каждым запросом исходящий текст сканируется повторно; если что-то
   осталось, запрос не отправляется (fail closed).

Суммы, даты, пороги и коды ответов не маскируются: без них нельзя проверить
логику требований.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


class RedactionLeak(RuntimeError):
    """В исходящем запросе осталось чувствительное значение."""


@dataclass
class Detection:
    kind: str
    start: int
    end: int
    value: str


# ---- проверки контрольных сумм ----


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iin_ok(digits: str) -> bool:
    """Контрольный разряд ИИН/БИН Казахстана."""
    if len(digits) != 12 or not digits.isdigit():
        return False
    a = [int(c) for c in digits]
    s = sum(a[i] * (i + 1) for i in range(11)) % 11
    if s == 10:
        w = [3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2]
        s = sum(a[i] * w[i] for i in range(11)) % 11
        if s == 10:
            return False
    return s == a[11]


def iban_ok(value: str) -> bool:
    v = re.sub(r"\s", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", v):
        return False
    v = v[4:] + v[:4]
    num = "".join(str(int(c, 36)) for c in v)
    return int(num) % 97 == 1


# ---- детекторы ----

KIND_LABEL = {
    "card": "КАРТА",
    "iin": "ИИН",
    "iban": "IBAN",
    "email": "EMAIL",
    "phone": "ТЕЛЕФОН",
    "ip": "IP",
    "url": "URL",
    "secret": "СЕКРЕТ",
    "person": "ФИО",
    "term": "ТЕРМИН",
}
_PLACEHOLDER = re.compile(r"\[\s*(" + "|".join(KIND_LABEL.values()) + r")_(\d+)\s*\]")

_RE_CARD = re.compile(r"(?<!\d)(?<!\d[.,])(?:\d[ -]?){12,18}\d(?!\d|[.,]\d)")
_RE_IIN = re.compile(r"(?<!\d)\d{12}(?!\d)")
_RE_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){12,30}\b")
_RE_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_RE_PHONE = re.compile(r"(?<![\w+])(?:\+7|8)[ \-(]*\d{3}[ \-)]*\d{3}[ \-]*\d{2}[ \-]*\d{2}(?!\d)")
_RE_IP = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
_RE_URL = re.compile(r"\b(?:https?|ftp)://[^\s<>\"'`|)]+", re.I)
_RE_SECRETS = [
    re.compile(r"\beyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,}"),  # JWT
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),  # Google API key
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"),  # OpenAI/Anthropic
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),  # GitHub
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS
    re.compile(r"(?i)\bbearer\s+(?!\$)[A-Za-z0-9._~+/\-]{20,}=*"),
    re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|token|пароль)\s*[=:]\s*(?![$<{])[^\s&'\"]{6,}"),
]
# «Фамилия И.О.» и «И.О. Фамилия» как подстраховка к NER
_RE_FIO = re.compile(
    r"\b[А-ЯЁ][а-яё]{2,}(?:ов|ев|ёв|ин|ын|ова|ева|ина|ына|ский|ская|цкий|цкая|ко|ук|юк|енко|ич|ян|ли|ұлы|қызы)\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.?"
    r"|\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]{2,}(?:ов|ев|ин|ова|ева|ина|ский|ская|ко|енко)\b"
)


@dataclass
class Redactor:
    enabled: bool = True
    use_ner: bool = True
    extra_terms: list[str] = field(default_factory=list)
    forward: dict[tuple[str, str], str] = field(default_factory=dict)
    backward: dict[str, str] = field(default_factory=dict)
    counters: Counter = field(default_factory=Counter)
    _ner: object = None

    @classmethod
    def from_env(cls, enabled: bool, use_ner: bool, terms_file: str | None) -> "Redactor":
        terms: list[str] = []
        if terms_file and Path(terms_file).exists():
            for ln in Path(terms_file).read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    terms.append(ln)
        return cls(enabled=enabled, use_ner=use_ner, extra_terms=terms)

    # ---- поиск ----

    def detect(self, text: str, with_ner: bool = True) -> list[Detection]:
        found: list[Detection] = []

        def add(kind: str, m: re.Match, value: str | None = None):
            found.append(Detection(kind, m.start(), m.end(), value if value is not None else m.group(0)))

        for m in _RE_URL.finditer(text):
            add("url", m, m.group(0).rstrip(".,;:"))
        for m in _RE_EMAIL.finditer(text):
            add("email", m)
        for rx in _RE_SECRETS:
            for m in rx.finditer(text):
                add("secret", m)
        for m in _RE_IBAN.finditer(text):
            if iban_ok(m.group(0)):
                add("iban", m)
        for m in _RE_CARD.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and luhn_ok(digits) and len(set(digits)) > 1:
                add("card", m)
        for m in _RE_IIN.finditer(text):
            if iin_ok(m.group(0)):
                add("iin", m)
        for m in _RE_PHONE.finditer(text):
            add("phone", m)
        for m in _RE_IP.finditer(text):
            add("ip", m)
        for m in _RE_FIO.finditer(text):
            add("person", m)
        for term in self.extra_terms:
            for m in re.finditer(re.escape(term), text, re.I):
                add("term", m)
        if with_ner and self.use_ner:
            found.extend(self._ner_persons(text))
        return _resolve_overlaps(found, text)

    def _ner_persons(self, text: str) -> list[Detection]:
        try:
            if self._ner is None:
                from natasha import Doc, NewsEmbedding, NewsNERTagger, Segmenter

                self._ner = (Segmenter(), NewsNERTagger(NewsEmbedding()), Doc)
            seg, tagger, Doc = self._ner
        except Exception:
            self.use_ner = False
            return []
        out: list[Detection] = []
        # Natasha обучена на прозе; размечаем построчно, чтобы таблицы не путали модель
        offset = 0
        for line in text.split("\n"):
            if re.search(r"[А-ЯЁ][а-яё]", line):
                d = Doc(line)
                d.segment(seg)
                d.tag_ner(tagger)
                for s in d.spans:
                    if s.type != "PER":
                        continue
                    val = line[s.start : s.stop]
                    # одиночное слово с маленькой буквы или латиница: не ФИО
                    if not re.match(r"[А-ЯЁ]", val) or _PLACEHOLDER.search(val):
                        continue
                    out.append(Detection("person", offset + s.start, offset + s.stop, val))
            offset += len(line) + 1
        return out

    # ---- маскирование ----

    def placeholder(self, kind: str, value: str) -> str:
        key = (kind, _canon(kind, value))
        if key not in self.forward:
            self.counters[kind] += 1
            ph = f"[{KIND_LABEL[kind]}_{self.counters[kind]}]"
            self.forward[key] = ph
            self.backward[ph] = value
        return self.forward[key]

    def redact(self, text: str) -> str:
        if not self.enabled:
            return text
        dets = self.detect(text)
        out: list[str] = []
        pos = 0
        for d in dets:
            out.append(text[pos : d.start])
            out.append(self.placeholder(d.kind, d.value))
            out.append(text[d.start + len(d.value) : d.end])
            pos = d.end
        out.append(text[pos:])
        return "".join(out)

    def restore(self, text: str) -> str:
        if not self.backward or not text:
            return text

        def sub(m: re.Match) -> str:
            ph = f"[{m.group(1)}_{m.group(2)}]"
            return self.backward.get(ph, m.group(0))

        return _PLACEHOLDER.sub(sub, text)

    def guard(self, outgoing: str) -> None:
        """Повторная проверка перед отправкой: без NER, только точные детекторы."""
        if not self.enabled:
            return
        clean = _PLACEHOLDER.sub("", outgoing)
        leaks = self.detect(clean, with_ner=False)
        # значения, которые уже замаскированы где-то ещё, тоже не должны встречаться
        for (kind, canon), _ph in self.forward.items():
            if kind == "person" and canon and canon in _canon("person", clean):
                leaks.append(Detection(kind, 0, 0, canon))
        if leaks:
            kinds = ", ".join(sorted({KIND_LABEL[d.kind] for d in leaks}))
            raise RedactionLeak(f"Запрос не отправлен: в нём остались чувствительные данные ({kinds}).")

    def stats(self) -> dict[str, int]:
        return {KIND_LABEL[k]: v for k, v in sorted(self.counters.items())}


def _canon(kind: str, value: str) -> str:
    if kind in {"card", "iin", "phone"}:
        return re.sub(r"\D", "", value)
    if kind == "iban":
        return re.sub(r"\s", "", value).upper()
    return " ".join(value.lower().split())


def _resolve_overlaps(dets: list[Detection], text: str) -> list[Detection]:
    """Оставляем непересекающиеся находки; при пересечении побеждает более длинная."""
    dets.sort(key=lambda d: (d.start, -(d.end - d.start)))
    out: list[Detection] = []
    for d in dets:
        if out and d.start < out[-1].end:
            if d.end - d.start > out[-1].end - out[-1].start:
                out[-1] = d
            continue
        out.append(d)
    return out
