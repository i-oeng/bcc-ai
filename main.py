"""Запуск агента Oresa из командной строки.

    python main.py --input samples/requirements.pdf --output reports/report.md

Без аргументов открывается интерактивный режим. То же самое: python -m validator ДОКУМЕНТ ОТЧЁТ.
Все параметры: python main.py --help
"""

import sys


def run() -> int:
    try:
        from validator.cli import main

        return main()
    except ModuleNotFoundError as e:
        # пакеты не установлены (запуск не из .venv): понятное сообщение вместо трассировки
        from validator.shell import human_error

        msg, todo = human_error(e)
        print(f"Ошибка: {msg}\n{todo}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nОстановлено.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(run())
