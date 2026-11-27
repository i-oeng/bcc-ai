"""Оценка проверки по эталонному списку дефектов.

    python eval/score.py runs/<проверка>/result.json [eval/gold.json] [review.json]

Без review.json выводит кандидатов для ручной проверки утверждений и цитат.
Для подсчёта полноты укажите решение по каждому дефекту: defects — объект
с ключами из эталона и значениями null (пропуск) или
{"finding_ids":["Н-01"],"credit":"full"}; для частичного совпадения credit="partial".
Для оценки точности блокеров укажите blockers — объект с ID каждого открытого
критичного нарушения и значением true (подтверждено) или false.
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


def candidates(defects, findings, grounder):
    for d in defects:
        locations = []
        for quote in d["anchors"]:
            a = grounder.find(quote, min_score=95)
            if a is None:
                print(f"  [!] Опора не найдена: {d['id']} «{quote}»")
            else:
                locations.append(set(range(a.line, a.line_end + 1)))
        pattern = re.compile(d["keywords"], re.I)
        hits = []
        for f in findings.values():
            if not pattern.search(f["problem"]):
                continue
            lines = {ln for a in f["anchors"] for ln in range(a["line"], a["line_end"] + 1)}
            count = sum(bool(lines & location) for location in locations)
            if count:
                hits.append((count, f["fid"]))
        hits.sort(key=lambda x: (-x[0], x[1]))
        yield d, hits


def adjudicated(defects, findings, review):
    credits = {"full": 1.0, "partial": 0.5}
    judgments = review["defects"]
    if set(judgments) != {d["id"] for d in defects}:
        raise ValueError("Для полноты нужна оценка по каждому ID эталона")

    scores = {}
    for defect_id, match in judgments.items():
        if match is None:
            scores[defect_id] = 0.0
            continue
        ids = match["finding_ids"]
        if not ids or any(fid not in findings for fid in ids):
            raise ValueError(f"{defect_id}: укажите ID подтверждённых уникальных находок")
        scores[defect_id] = credits[match["credit"]]

    for label, group in (("Все дефекты", defects), ("Ключевые", [d for d in defects if d["core"]])):
        if group:
            full = sum(scores[d["id"]] == 1 for d in group)
            partial = sum(scores[d["id"]] == 0.5 for d in group)
            print(f"{label}: полнота {full}/{len(group)} ({full / len(group):.0%}), "
                  f"частично {partial}, с учётом частичных {(full + 0.5 * partial) / len(group):.0%}")
    if "blockers" in review:
        blockers = {fid for fid, f in findings.items()
                    if f["severity"] == "critical" and f.get("resolution") != "closed"}
        verdicts = review["blockers"]
        if set(verdicts) != blockers or any(type(v) is not bool for v in verdicts.values()):
            raise ValueError("Для точности нужна оценка true/false по каждому открытому блокеру")
        if blockers:
            good = sum(verdicts.values())
            print(f"Точность блокеров: {good}/{len(blockers)} ({good / len(blockers):.0%})")
        else:
            print("Точность блокеров: открытых блокеров нет")


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
    expected_hash = result["document"].get("sha256")
    if expected_hash and expected_hash != doc.sha256:
        print("Исходный файл изменился. Для сверки нужна версия из проверенного запуска.", file=sys.stderr)
        return 2
    g = Grounder(doc)

    kept = {f["fid"]: f for f in result["findings"] if f["kept"] and not f["duplicate_of"]}
    models = ", ".join(sorted({m for _, m in result.get("steps", [])})) or result.get("model")
    print(f"Документ: {doc.path.name} / проверка: {sys.argv[1]} / модели: {models}")
    if len(sys.argv) > 3:
        try:
            review = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
            adjudicated(gold["defects"], kept, review)
        except (KeyError, ValueError, TypeError) as e:
            print(f"Некорректная разметка проверки: {e}", file=sys.stderr)
            return 2
    else:
        print("\nКандидаты для проверки вручную (ID находки и число совпавших опор)")
        for d, hits in candidates(gold["defects"], kept, g):
            labels = ", ".join(f"{fid} ({n}/{len(d['anchors'])})" for n, fid in hits)
            print(f"  {d['id']}{'*' if d['core'] else ' '} {d['title']}  <- {labels or 'нет кандидата'}")
        print(f"\nУникальные подтверждённые находки ({len(kept)})")
        for f in kept.values():
            locations = ", ".join(f"стр. {a['line']}" for a in f["anchors"])
            print(f"  {f['fid']} [{f['severity']}] {f['problem']} ({locations})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
