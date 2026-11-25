"""Командная строка.

    python -m validator ВХОДНОЙ_ФАЙЛ [ФАЙЛ_ОТЧЁТА]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True)
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        prog="python -m validator",
        description="ИИ-агент статической проверки требований (ISO/IEC/IEEE 29148, IEEE 830). "
        "Читает документ (PDF, DOCX, MD, TXT, HTML) и пишет отчёт на русском в Markdown.",
    )
    ap.add_argument("input", nargs="?", help="путь к документу с требованиями")
    ap.add_argument("output", nargs="?", help="путь к файлу отчёта (по умолчанию reports/<имя>_report.md)")
    ap.add_argument("-o", "--out", dest="out_opt", help="то же, что второй аргумент")
    ap.add_argument("--model", help="модель Gemini (по умолчанию из GEMINI_MODEL в .env)")
    ap.add_argument("--fallback", help="запасные модели через запятую (по умолчанию GEMINI_FALLBACK_MODELS)")
    ap.add_argument("--batch-size", type=int, help="сколько требований проверять за один запрос (по умолчанию 12)")
    ap.add_argument("--no-redact", action="store_true", help="не маскировать чувствительные данные (не рекомендуется)")
    ap.add_argument("--no-cache", action="store_true", help="не брать ответы модели из кэша (полностью новый прогон)")
    ap.add_argument("--dry-run", action="store_true",
                    help="ничего не отправлять в модель: только показать, что ушло бы (маскированный документ и первый запрос)")
    ap.add_argument("--list-models", action="store_true", help="показать модели, доступные вашему ключу")
    ap.add_argument("--rerender", metavar="RUN_DIR",
                    help="перерисовать отчёт из сохранённого прогона runs/<...> без запросов к модели")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    from .config import Config
    from .llm import LLMError
    from .redact import RedactionLeak

    cfg = Config.load(
        model=args.model,
        fallbacks=[m.strip() for m in args.fallback.split(",") if m.strip()] if args.fallback is not None else None,
        batch_size=args.batch_size,
        redact=False if args.no_redact else None,
    )
    if args.no_cache:
        cfg.cache_dir = None

    if args.list_models:
        from .llm import GeminiClient
        from .redact import Redactor

        try:
            for m in GeminiClient(cfg.api_key, cfg.model, Redactor()).list_models():
                print(m)
        except LLMError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            return 2
        return 0

    if args.rerender:
        from . import pipeline, report

        res = pipeline.load_state(args.rerender)
        out = args.input or args.output or args.out_opt or str(Path("reports") / f"{res.doc.path.stem}_report.md")
        print(f"Отчёт: {report.write(res, out)}")
        return 0

    if not args.input:
        ap.print_help()
        return 2

    out = args.output or args.out_opt or str(Path("reports") / f"{Path(args.input).stem}_report.md")

    from . import pipeline, report

    try:
        res = pipeline.run(args.input, cfg, dry_run=args.dry_run)
        if args.dry_run:
            return 0
        path = report.write(res, out)
    except (LLMError, RedactionLeak, FileNotFoundError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    kept = res.kept()
    crit = sum(1 for f in kept if f.severity == "critical")
    print()
    print(f"Отчёт: {path}")
    print(f"Требований: {len(res.requirements)}, нарушений: {len(kept)} (критичных {crit}), вопросов: {len(res.questions)}")
    print(f"Разработка: {res.decision_dev}")
    print(f"Журнал запросов к модели: {res.audit_dir}")
    return 0
