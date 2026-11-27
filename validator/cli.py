"""Командная строка.

    python -m validator ВХОДНОЙ_ФАЙЛ [ФАЙЛ_ОТЧЁТА]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import APP_NAME, __version__


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True)
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        prog="python -m validator",
        description=f"{APP_NAME} проверяет требования по ISO/IEC/IEEE 29148, IEEE 830 и другим стандартам. "
        "Читает документ (PDF, DOCX, MD, TXT, HTML) и пишет отчёт на русском в Markdown.",
    )
    ap.add_argument("input", nargs="?", help="путь к документу с требованиями")
    ap.add_argument("output", nargs="?", help="путь к файлу отчёта (по умолчанию reports/<имя>_report.md)")
    ap.add_argument("-o", "--out", dest="out_opt", help="то же, что второй аргумент")
    ap.add_argument("--provider", choices=["gemini", "openai", "anthropic"],
                    help="провайдер модели (по умолчанию LLM_PROVIDER из .env, иначе gemini)")
    ap.add_argument("--model", help="модель (по умолчанию <ПРОВАЙДЕР>_MODEL из .env)")
    ap.add_argument("--fallback", help="запасные модели через запятую (по умолчанию <ПРОВАЙДЕР>_FALLBACK_MODELS)")
    ap.add_argument("--base-url", help="адрес OpenAI-совместимого сервера (OpenRouter, DeepSeek, Groq, vLLM, Ollama)")
    ap.add_argument("--standards", help="стандарты через запятую, например ieee830,ears (ISO 29148 всегда включён); "
                    "список: --list-standards")
    ap.add_argument("--list-standards", action="store_true", help="показать доступные стандарты")
    ap.add_argument("--parallel", type=int, help="сколько запросов к модели идут одновременно (по умолчанию 6)")
    ap.add_argument("--batch-size", type=int, help="сколько требований проверять за один запрос (по умолчанию 12)")
    ap.add_argument("--no-redact", action="store_true", help="не маскировать чувствительные данные (не рекомендуется)")
    ap.add_argument("--no-cache", action="store_true", help="не брать ответы модели из кэша (полностью новая проверка)")
    ap.add_argument("--answers", metavar="FILE",
                    help="файл вопросов с ответами заказчика (<отчёт>_questions.md): агент закроет дефекты, которые ответы устраняют")
    ap.add_argument("--fix", action="store_true",
                    help="дополнительно сформировать исправленную редакцию документа (<отчёт>_fixed.md)")
    ap.add_argument("--dry-run", action="store_true",
                    help="ничего не отправлять в модель: только показать, что ушло бы (маскированный документ и первый запрос)")
    ap.add_argument("--list-models", action="store_true", help="показать модели, доступные вашему ключу")
    ap.add_argument("--rerender", metavar="RUN_DIR",
                    help="перерисовать отчёт из сохранённой проверки runs/<...> без запросов к модели")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = ap.parse_args(argv)

    from .config import Config
    from .criteria import StandardError, available, profile
    from .llm import Cancelled, LLMError, make_backend
    from .redact import RedactionLeak

    if args.list_standards:
        for s in sorted(available().values(), key=lambda s: s.order):
            print(f"{s.id:14s} {s.title}{' (всегда включён)' if s.mandatory else ''}: {s.description}")
        return 0

    try:
        cfg = Config.load(
            provider=args.provider,
            model=args.model,
            fallbacks=[m.strip() for m in args.fallback.split(",") if m.strip()] if args.fallback is not None else None,
            batch_size=args.batch_size,
            redact=False if args.no_redact else None,
            base_url=args.base_url,
            standards=[s.strip() for s in args.standards.split(",") if s.strip()] if args.standards else None,
            parallel=args.parallel,
        )
        profile(cfg.standards)  # неизвестный стандарт: ошибка до запросов к модели
    except (ValueError, StandardError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    if args.no_cache:
        cfg.cache_dir = None

    if args.list_models:
        try:
            for m in make_backend(cfg.provider, cfg.api_key, cfg.base_url).list_models():
                print(m)
        except Exception as e:  # noqa: BLE001
            print(f"Ошибка: {e}", file=sys.stderr)
            return 2
        return 0

    if args.rerender:
        from . import pipeline, report

        res = pipeline.load_state(args.rerender)
        out = args.input or args.output or args.out_opt or str(Path("reports") / f"{res.doc.path.stem}_report.md")
        path = report.write(res, out)
        print(f"Отчёт: {path}")
        for label, p in report.write_extras(res, path).items():
            print(f"{label}: {p}")
        return 0

    if not args.input:
        ap.print_help()
        return 2

    out = args.output or args.out_opt or str(Path("reports") / f"{Path(args.input).stem}_report.md")

    from . import pipeline, report
    from .loader import DocumentError

    try:
        res = pipeline.run(args.input, cfg, dry_run=args.dry_run, answers_path=args.answers, fix=args.fix)
        if args.dry_run:
            return 0
        path = report.write(res, out)
        extra = report.write_extras(res, path)
    except DocumentError as e:
        print(f"Документ нельзя проверить: {e}", file=sys.stderr)
        if not args.dry_run:
            print(f"Диагностика записана в отчёт: {report.write_diagnostic(args.input, e, out)}")
        return 2
    except (LLMError, RedactionLeak, FileNotFoundError, ValueError, Cancelled) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    kept = res.open_findings()
    crit = sum(1 for f in kept if f.severity == "critical")
    print()
    print(f"Отчёт: {path}")
    for label, p in extra.items():
        print(f"{label}: {p}")
    closed = len(res.kept()) - len(kept)
    print(f"Требований: {len(res.requirements)}, открытых нарушений: {len(kept)} (критичных {crit})"
          + (f", закрыто ответами: {closed}" if closed else "") + f", вопросов: {len(res.questions)}")
    print(f"Разработка: {res.decision_dev}")
    print(f"Журнал запросов к модели: {res.audit_dir}")
    if res.finished:
        print(f"Время проверки: {(res.finished - res.started).total_seconds():.0f} с")
    return 0
