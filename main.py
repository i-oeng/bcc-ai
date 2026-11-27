"""Агент проверки требований: запуск одной командой.

    python main.py --input samples/requirements.pdf --output reports/report.md

То же самое: python -m validator samples/requirements.pdf reports/report.md
Все параметры: python main.py --help
"""

import sys

from validator.cli import main

if __name__ == "__main__":
    sys.exit(main())
