"""Оценка прогона по эталонному списку дефектов.

    python eval/score.py runs/<прогон>/result.json [eval/gold.json]

Полнота (recall): какая доля эталонных дефектов найдена подтверждёнными
находками агента. Находки, не совпавшие ни с одним эталонным дефектом,
печатаются отдельно: это либо ложные срабатывания, либо дефекты, которых
нет в эталоне (их нужно просмотреть вручную).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from validator.grounding import Grounder  # noqa: E402
from validator.loader import load  # noqa: E402


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    gold = json.loads(Path(sys.argv[2] if len(sys.argv) > 2 else ROOT / "eval" / "gold.json").read_text(encoding="utf-8"))
    doc_path = Path(result["document"]["path"])
    doc = load(doc_path if doc_path.is_absolute() else ROOT / doc_path)
    g = Grounder(doc)

    kept = [f for f in result["findings"] if f["kept"]]
    matched_findings: set[str] = set()
    found, missed = [], []
    for d in gold["defects"]:
        lines: set[int] = set()
        for a in d["anchors"]:
            an = g.find(a, min_score=95)
            if an is None:
                print(f"  [!] опора не найдена в документе: {d['id']} «{a}»")
                continue
            lines.update(range(an.line, an.line_end + 1))
        kw = re.compile(d["keywords"], re.I)
        hits = []
        for f in kept:
            flines = {ln for a in f["anchors"] for ln in range(a["line"], a["line_end"] + 1)}
            if flines & lines and kw.search(f["problem"] + " " + f["fix"]):
                hits.append(f["fid"])
        if hits:
            found.append((d, hits))
            matched_findings.update(hits)
        else:
            missed.append(d)

    total = len(gold["defects"])
    core = [d for d in gold["defects"] if d["core"]]
    core_found = [d for d, _ in found if d["core"]]
    print(f"Документ: {doc.path.name}; прогон: {sys.argv[1]}; модель: {result.get('model')}")
    print(f"Полнота: {len(found)}/{total} = {len(found) / total:.0%}; по ключевым (core): "
          f"{len(core_found)}/{len(core)} = {len(core_found) / len(core):.0%}")
    print(f"Подтверждённых находок: {len(kept)} (уникальных {sum(1 for f in kept if not f['duplicate_of'])}); "
          f"совпали с эталоном: {len(matched_findings)}")
    print("\nНайдено:")
    for d, hits in found:
        print(f"  + {d['id']}{'*' if d['core'] else ' '} {d['title'][:90]}  <- {', '.join(hits)}")
    print("\nПропущено:")
    for d in missed:
        print(f"  - {d['id']}{'*' if d['core'] else ' '} {d['title'][:110]}")
    extra = [f for f in kept if f["fid"] not in matched_findings and not f["duplicate_of"]]
    print(f"\nНаходки вне эталона ({len(extra)}), проверить вручную:")
    for f in extra:
        print(f"  ? {f['fid']} [{f['severity']}/{f['criterion']}] {f['problem'][:130]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
