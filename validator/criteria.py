"""Критерии качества требований и профили стандартов.

Каждый стандарт описан файлом validator/standards/<id>.json: какие критерии он
добавляет и на какие пункты ссылается. Профиль проверки собирается из выбранных
стандартов: ISO/IEC/IEEE 29148 обязателен (базовая модель качества), остальные
добавляют свои критерии или ссылки к существующим. Свои стандарты можно положить
в папку из VALIDATOR_STANDARDS_DIR.

Формат критерия в файле стандарта:
    {"code": "...", "scope": "requirement" | "set", "name": "...", "short": "...",
     "source": "пункт стандарта", "question": "что проверяет агент"}
Если code уже задан другим выбранным стандартом, запись только дописывает
source (и question, если указан) к существующему критерию. Коды критериев
набора начинаются с set_.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

BUILTIN_DIR = Path(__file__).resolve().parent / "standards"
DEFAULT_STANDARDS = ["iso29148", "ieee830"]
BASE_STANDARD = "iso29148"


@dataclass(frozen=True)
class Criterion:
    code: str
    name: str
    source: str
    question: str  # что проверяет агент
    scope: str = "requirement"  # requirement | set
    short: str = ""  # заголовок колонки в матрице отчёта

    @property
    def column(self) -> str:
        return self.short or self.name[:6] + "."


@dataclass(frozen=True)
class Standard:
    id: str
    title: str
    cite: str
    description: str
    mandatory: bool
    criteria: tuple
    path: str = ""
    order: int = 100


@dataclass
class Profile:
    standards: list[Standard]
    requirement: list[Criterion]
    set: list[Criterion]
    by_code: dict[str, Criterion] = field(default_factory=dict)

    @property
    def ids(self) -> list[str]:
        return [s.id for s in self.standards]

    def name(self, code: str) -> str:
        c = self.by_code.get(code) or _any_criterion(code)
        return c.name if c else code

    def has(self, code: str) -> bool:
        return code in self.by_code

    def cites(self) -> str:
        """«A, B и C» для промптов и отчёта."""
        items = [s.cite for s in self.standards]
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " и " + items[-1]

    def titles(self) -> str:
        return ", ".join(s.title for s in self.standards)


class StandardError(ValueError):
    pass


def _dirs() -> list[Path]:
    dirs = [BUILTIN_DIR]
    extra = os.getenv("VALIDATOR_STANDARDS_DIR", "").strip()
    if extra:
        dirs.append(Path(extra))
    return dirs


@lru_cache(maxsize=8)
def _load_all(dirs: tuple[str, ...]) -> dict[str, Standard]:
    out: dict[str, Standard] = {}
    for d in dirs:
        p = Path(d)
        if not p.is_dir():
            continue
        for f in sorted(p.glob("*.json")):
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                sid = raw["id"].strip()
                crit = tuple(raw.get("criteria", []))
                for c in crit:
                    if "code" not in c:
                        raise StandardError(f"{f.name}: у критерия нет code")
                    if c.get("scope") == "set" and not c["code"].startswith("set_"):
                        raise StandardError(f"{f.name}: код критерия набора должен начинаться с set_: {c['code']}")
                out[sid] = Standard(sid, raw.get("title", sid), raw.get("cite", raw.get("title", sid)),
                                    raw.get("description", ""), bool(raw.get("mandatory")), crit, str(f),
                                    int(raw.get("order", 100)))
            except (KeyError, json.JSONDecodeError) as e:
                raise StandardError(f"Файл стандарта {f} повреждён: {e}") from e
    return out


def available() -> dict[str, Standard]:
    return _load_all(tuple(str(d) for d in _dirs()))


def _any_criterion(code: str) -> Criterion | None:
    """Имя критерия для старых проверок и сравнения: ищем по всем известным стандартам."""
    for sid in available():
        try:
            c = _profile_cached((BASE_STANDARD,) if sid == BASE_STANDARD else (BASE_STANDARD, sid)).by_code.get(code)
        except StandardError:
            continue
        if c:
            return c
    return None


def normalize_ids(ids) -> tuple[str, ...]:
    """Базовый стандарт всегда первым, остальные в постоянном порядке: тот же выбор даёт те же промпты и кэш."""
    ids = [i.strip() for i in (ids or DEFAULT_STANDARDS) if i and i.strip()]
    known = available()
    rest = sorted({i for i in ids if i != BASE_STANDARD}, key=lambda i: (known[i].order if i in known else 999, i))
    return tuple([BASE_STANDARD] + rest)


def profile(ids=None) -> Profile:
    return _profile_cached(normalize_ids(ids))


@lru_cache(maxsize=32)
def _profile_cached(ids: tuple[str, ...]) -> Profile:
    known = available()
    missing = [i for i in ids if i not in known]
    if missing:
        raise StandardError(f"Неизвестный стандарт: {', '.join(missing)}. Доступны: {', '.join(known)}")
    order: list[str] = []
    data: dict[str, dict] = {}
    for sid in ids:
        for c in known[sid].criteria:
            code = c["code"]
            if code in data:
                d = data[code]
                if c.get("source"):
                    d["source"] = "; ".join(x for x in (d["source"], c["source"]) if x)
                if c.get("question"):
                    d["question"] = (d["question"] + " " + c["question"]).strip()
                continue
            if not (c.get("name") and c.get("question")):
                raise StandardError(f"Стандарт {sid}: критерий {code} дополняет другой стандарт, который не выбран")
            data[code] = {"code": code, "name": c["name"], "source": c.get("source", ""), "question": c["question"],
                          "scope": c.get("scope", "set" if code.startswith("set_") else "requirement"),
                          "short": c.get("short", "")}
            order.append(code)
    crit = [Criterion(**data[code]) for code in order]
    return Profile(
        standards=[known[i] for i in ids],
        requirement=[c for c in crit if c.scope == "requirement"],
        set=[c for c in crit if c.scope == "set"],
        by_code={c.code: c for c in crit},
    )


# Профиль по умолчанию (ISO 29148 + IEEE 830): для старого кода, eval и проверок без профиля.
DEFAULT = profile(DEFAULT_STANDARDS)
REQUIREMENT_CRITERIA: list[Criterion] = DEFAULT.requirement
SET_CRITERIA: list[Criterion] = DEFAULT.set
ALL = DEFAULT.by_code

SEVERITIES = {
    "critical": "Критично",
    "major": "Существенно",
    "minor": "Незначительно",
}

SEVERITY_RULES = """\
critical (Критично): блокирует разработку или тестирование: противоречие между требованиями или с критериями приёмки; \
требование непроверяемо; без ответа на вопрос невозможно реализовать или написать тест; риск денег клиента, безопасности, \
персональных данных.
major (Существенно): высокий риск неверной реализации или переделки: неоднозначность, пропущенный сценарий или граничный \
случай, лишнее проектное решение, нарушение семантики интерфейса.
minor (Незначительно): оформление, стиль, шаблон, мелкая неточность без риска неверной реализации."""


def name(code: str) -> str:
    c = ALL.get(code) or _any_criterion(code)
    return c.name if c else code


def criteria_block(items: list[Criterion]) -> str:
    return "\n".join(f"- {c.code}: {c.name} ({c.source}): {c.question}" for c in items)
