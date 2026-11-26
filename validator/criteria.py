"""Критерии качества требований и профили стандартов.

Каждый стандарт описан файлом validator/standards/<id>.json: какие критерии он
добавляет и на какие пункты ссылается. Профиль проверки собирается из выбранных
стандартов: ISO/IEC/IEEE 29148 обязателен (базовая модель качества), остальные
добавляют свои критерии или ссылки к существующим. Свои стандарты и изменённые
встроенные лежат в custom_standards/ (или в папке из VALIDATOR_STANDARDS_DIR):
файл с тем же id заменяет встроенный, удаление файла возвращает исходный.

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
import re
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


def custom_dir() -> Path:
    """Папка своих и изменённых стандартов. Файл с тем же id, что у встроенного, заменяет встроенный."""
    extra = os.getenv("VALIDATOR_STANDARDS_DIR", "").strip()
    return Path(extra) if extra else BUILTIN_DIR.parent.parent / "custom_standards"


def _dirs() -> list[Path]:
    return [BUILTIN_DIR, custom_dir()]


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


def builtin_ids() -> set[str]:
    return {f.stem for f in BUILTIN_DIR.glob("*.json")}


def reload() -> None:
    """Сбросить кэш после изменения файлов стандартов."""
    _load_all.cache_clear()
    _profile_cached.cache_clear()


# ---------- редактирование стандартов (веб-интерфейс) ----------

_ID = re.compile(r"^[a-z0-9_]{2,40}$")
SCOPES = ("requirement", "set")


def raw_standard(sid: str) -> dict:
    """Действующий JSON стандарта (изменённый, если есть, иначе встроенный)."""
    s = available().get(sid)
    if not s:
        raise StandardError(f"Стандарт {sid!r} не найден")
    return json.loads(Path(s.path).read_text(encoding="utf-8"))


def builtin_standard(sid: str) -> dict | None:
    p = BUILTIN_DIR / f"{sid}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def clean_standard(raw: dict) -> dict:
    """Проверить и привести стандарт к формату файла. Ошибки на русском, с номером критерия."""
    if not isinstance(raw, dict):
        raise StandardError("Стандарт должен быть объектом JSON")
    sid = str(raw.get("id", "")).strip().lower()
    if not _ID.match(sid):
        raise StandardError("Идентификатор: 2-40 символов, латинские строчные буквы, цифры и _")
    title = str(raw.get("title", "")).strip()
    if not title:
        raise StandardError("Укажите название стандарта")
    try:
        order = int(raw.get("order", 100))
    except (TypeError, ValueError):
        raise StandardError("Порядок должен быть числом") from None
    crit_in = raw.get("criteria")
    if not isinstance(crit_in, list) or not crit_in:
        raise StandardError("Добавьте хотя бы один критерий")
    known = {c["code"] for s in available().values() if s.id != sid for c in s.criteria}
    out, seen = [], set()
    for n, c in enumerate(crit_in, 1):
        if not isinstance(c, dict):
            raise StandardError(f"Критерий {n}: неверный формат")
        code = str(c.get("code", "")).strip().lower()
        scope = str(c.get("scope") or ("set" if code.startswith("set_") else "requirement"))
        if not _ID.match(code):
            raise StandardError(f"Критерий {n}: код из латинских строчных букв, цифр и _ (2-40 символов)")
        if scope not in SCOPES:
            raise StandardError(f"Критерий {n}: область должна быть requirement или set")
        if scope == "set" and not code.startswith("set_"):
            raise StandardError(f"Критерий {n}: код критерия набора начинается с set_ (например set_{code})")
        if scope == "requirement" and code.startswith("set_"):
            raise StandardError(f"Критерий {n}: код set_… только для критериев набора")
        if code in seen:
            raise StandardError(f"Критерий {n}: код {code} уже есть в этом стандарте")
        seen.add(code)
        item = {"code": code, "scope": scope}
        for k in ("name", "short", "source", "question"):
            v = " ".join(str(c.get(k) or "").split())
            if v:
                item[k] = v
        if not (item.get("name") and item.get("question")) and code not in known:
            raise StandardError(f"Критерий {n} ({code}): заполните название и что проверяет агент "
                                "(пустыми их можно оставить, только если критерий дополняет другой стандарт)")
        out.append(item)
    res = {"id": sid, "order": order, "title": title,
           "cite": " ".join(str(raw.get("cite") or title).split()),
           "description": " ".join(str(raw.get("description") or "").split()), "criteria": out}
    if sid == BASE_STANDARD:
        res["mandatory"] = True
    return res


def save_standard(raw: dict, expected_id: str | None = None) -> Standard:
    """Записать свой стандарт или изменённый встроенный в custom_dir(). Встроенные файлы не трогаем."""
    data = clean_standard(raw)
    sid = data["id"]
    if expected_id is not None and sid != expected_id:
        raise StandardError("Идентификатор стандарта менять нельзя: создайте новый стандарт")
    d = custom_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.json"
    old = p.read_text(encoding="utf-8") if p.is_file() else None
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    reload()
    try:  # стандарт должен собираться в профиль: и сам, и в паре с базовым
        profile([sid])
        if sid == BASE_STANDARD:
            profile(DEFAULT_STANDARDS)
    except StandardError:
        if old is None:
            p.unlink()
        else:
            p.write_text(old, encoding="utf-8")
        reload()
        raise
    return available()[sid]


def delete_standard(sid: str) -> str:
    """Удалить свой стандарт или вернуть встроенный к исходному. Возвращает 'reset' или 'deleted'."""
    p = custom_dir() / f"{sid}.json"
    if Path(sid).name != sid or not p.is_file():
        raise StandardError("Встроенный стандарт без изменений: удалять нечего")
    builtin = sid in builtin_ids()
    if not builtin:
        own = {c["code"] for c in available()[sid].criteria}
        users = sorted({s.id for s in available().values() if s.id != sid
                        for c in s.criteria if c["code"] in own and not c.get("name")})
        if users:
            raise StandardError(f"Критерии этого стандарта дополняет {', '.join(users)}")
    p.unlink()
    reload()
    return "reset" if builtin else "deleted"


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
