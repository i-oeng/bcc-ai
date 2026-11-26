"""Сравнение двух проверок: исходный документ против исправленной редакции.

    python eval/compare.py runs/<исходный>/result.json runs/<исправленный>/result.json

Печатает таблицу в Markdown: сколько требований, открытых нарушений по
серьёзности и критериям, вопросов и пунктов чек-листа в каждой проверке.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from validator.criteria import ALL  # noqa: E402

SEV = [("critical", "🔴 критичных"), ("major", "🟠 существенных"), ("minor", "🟡 незначительных")]


def stats(path: str) -> dict:
    r = json.loads(Path(path).read_text(encoding="utf-8"))
    open_ = [f for f in r["findings"] if f["kept"] and not f["duplicate_of"] and f.get("resolution") != "closed"]
    return {
        "name": Path(r["document"]["path"]).name,
        "model": ", ".join(sorted({m for _, m in r.get("steps", [])})) or r.get("model"),
        "requirements": len(r["requirements"]),
        "open": len(open_),
        "sev": Counter(f["severity"] for f in open_),
        "crit": Counter(f["criterion"] for f in open_),
        "questions": len(r.get("questions", [])),
        "blocking": sum(1 for q in r.get("questions", []) if q.get("priority") == "blocking"),
        "checklist_yes": sum(1 for c in r.get("checklist", []) if c["answer"].strip().lower() == "да"),
        "checklist": len(r.get("checklist", [])),
        "dev": r["decision"]["development"],
    }


def main() -> int:
    for s in (sys.stdout,):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    a, b = stats(sys.argv[1]), stats(sys.argv[2])
    rows = [
        ("Документ", a["name"], b["name"]),
        ("Модель", a["model"], b["model"]),
        ("Требований", a["requirements"], b["requirements"]),
        ("Открытых нарушений", a["open"], b["open"]),
        *[(label, a["sev"][k], b["sev"][k]) for k, label in SEV],
        ("Вопросов заказчику (блокирующих)", f"{a['questions']} ({a['blocking']})", f"{b['questions']} ({b['blocking']})"),
        ("Чек-лист ревью: «Да»", f"{a['checklist_yes']} из {a['checklist']}", f"{b['checklist_yes']} из {b['checklist']}"),
        ("Разработка", a["dev"], b["dev"]),
    ]
    print("| Показатель | До | После |\n| :--- | :--- | :--- |")
    for k, x, y in rows:
        print(f"| {k} | {x} | {y} |")
    print("\n| Критерий | До | После |\n| :--- | :---: | :---: |")
    for code in sorted(set(a["crit"]) | set(b["crit"]), key=lambda c: -a["crit"][c]):
        print(f"| {ALL[code].name if code in ALL else code} | {a['crit'][code]} | {b['crit'][code]} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
