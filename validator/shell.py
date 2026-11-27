"""Интерактивный режим в терминале: python main.py (без аргументов).

Перетащите документ в окно или введите путь: агент проверит его и покажет итог. Дальше можно
спрашивать о результате обычными словами или вызывать команды (/help). Только стандартная
библиотека: ничего не нужно ставить сверх requirements.txt.
"""

from __future__ import annotations

import os
import queue
import re
import shlex
import shutil
import sys
import threading
import time
from pathlib import Path

# ---------- палитра ----------

_COLOR = (sys.stdout.isatty() or bool(os.getenv("FORCE_COLOR"))) and not os.getenv("NO_COLOR")
if _COLOR and os.name == "nt":
    os.system("")  # включить ANSI-последовательности в консоли Windows
_TRUE = _COLOR and bool(os.getenv("WT_SESSION") or os.getenv("COLORTERM") in ("truecolor", "24bit")
                        or os.getenv("TERM_PROGRAM") in ("vscode", "iTerm.app", "WezTerm"))


def _fg(rgb: tuple[int, int, int], basic: str):
    if not _COLOR:
        return lambda s: s
    code = f"38;2;{rgb[0]};{rgb[1]};{rgb[2]}" if _TRUE else basic
    return lambda s: f"\033[{code}m{s}\033[39m"


def _badge(bg: tuple[int, int, int], basic_bg: str, fg: str = "97"):
    if not _COLOR:
        return lambda s: f"[{s.strip()}]"
    code = f"48;2;{bg[0]};{bg[1]};{bg[2]}" if _TRUE else basic_bg
    return lambda s: f"\033[{code};{fg};1m{s}\033[0m"


def _attr(on: str, off: str):
    return (lambda s: f"\033[{on}m{s}\033[{off}m") if _COLOR else (lambda s: s)


bold, dim, italic = _attr("1", "22"), _attr("2", "22"), _attr("3", "23")
green = _fg((0, 171, 117), "32")      # фирменный зелёный BCC
gold = _fg((220, 168, 110), "33")     # фирменное золото BCC
red = _fg((235, 87, 87), "91")
amber = _fg((242, 169, 59), "93")
grey = _fg((135, 138, 150), "90")
cyan = _fg((110, 190, 230), "96")
yellow = amber
B_CRIT = _badge((196, 52, 52), "41")
B_MAJ = _badge((214, 145, 30), "43", "30")
B_MIN = _badge((88, 92, 104), "100")
B_OK = _badge((0, 140, 96), "42")
SEV = {"critical": ("КРИТ", B_CRIT, red), "major": ("СУЩ ", B_MAJ, amber), "minor": ("НЕЗН", B_MIN, grey)}
PRIO = {"blocking": ("БЛОК", B_CRIT), "important": ("ВАЖН", B_MAJ), "nice": ("ЖЕЛ ", B_MIN)}
SEV_ARG = {"крит": "critical", "критичные": "critical", "critical": "critical", "сущ": "major", "существенные": "major",
           "major": "major", "незн": "minor", "незначительные": "minor", "minor": "minor"}
ID_RE = re.compile(r"^\s*([НнВв]|ТР|тр)-?(\d+)\s*$")
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ANSI = re.compile(r"\033\[[0-9;]*m")


# ---------- вёрстка ----------


def width() -> int:
    return max(60, min(shutil.get_terminal_size((100, 30)).columns, 110))


def vlen(s: str) -> int:
    return len(ANSI.sub("", s))


def wrap(text: str, w: int) -> list[str]:
    """Перенос по видимой ширине: цветовые коды не считаются символами."""
    w = max(10, w)
    out: list[str] = []
    for para in str(text).split("\n"):
        line, n = "", 0
        for word in para.split(" "):
            k = vlen(word)
            if n and n + 1 + k > w:
                out.append(line)
                line, n = word, k
            elif line or n:
                line, n = line + " " + word, n + 1 + k
            else:
                line, n = word, k
        out.append(line)
    return out


def say(text: str = "", indent: int = 2, style=None) -> None:
    """Абзац с переносом по ширине терминала."""
    for ln in wrap(text, width() - indent - 1):
        print(" " * indent + (style(ln) if style else ln))


def hang(prefix: str, text: str, indent: int = 2, style=None) -> None:
    """Строка с префиксом (значок, ID) и висячим отступом для переносов."""
    pw = vlen(prefix)
    lines = wrap(text, width() - indent - pw - 1)
    for i, ln in enumerate(lines):
        lead = prefix if i == 0 else " " * pw
        print(" " * indent + lead + (style(ln) if style else ln))


def md(s: str) -> str:
    """**жирный** и `код` из ответов модели в оформление терминала."""
    s = re.sub(r"\*\*(.+?)\*\*", lambda m: bold(m.group(1)), s)
    return re.sub(r"`([^`]+)`", lambda m: cyan(m.group(1)), s)


def short_path(p: Path, n: int) -> str:
    """Путь коротко: домашняя папка как ~, длинная середина как …"""
    s = str(p)
    home = str(Path.home())
    if s.lower().startswith(home.lower()):
        s = "~" + s[len(home):]
    if len(s) <= n:
        return s
    sep = "\\" if "\\" in s else "/"
    parts = s.split(sep)
    head, tail = parts[0], parts[-1]
    out = tail
    for part in reversed(parts[1:-1]):  # добавляем папки с конца, пока помещается
        if len(head) + len(part) + len(out) + 4 > n:
            break
        out = part + sep + out
    return f"{head}{sep}…{sep}{out}" if out != sep.join(parts[1:]) else s


def clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n * 0.6 else cut).rstrip(" ,;:.") + "…"


def panel(rows: list[str], title: str = "", color=grey, right: str = "") -> None:
    """Рамка со скруглёнными углами; строки уже свёрстаны по внутренней ширине (inner())."""
    w = width() - 4
    fill = w - 2 - (vlen(title) + 3 if title else 0) - (vlen(right) + 3 if right else 0)
    top = (color("╭") + (color("─ ") + title + " " if title else "") + color("─" * max(1, fill))
           + (" " + right + color(" ─") if right else "") + color("╮"))
    print("  " + top)
    for r in rows:
        print("  " + color("│") + " " + r + " " * max(0, w - 4 - vlen(r)) + " " + color("│"))
    print("  " + color("╰" + "─" * (w - 2) + "╯"))


def inner() -> int:
    return width() - 8


def boxed(text: str, prefix: str = "", style=None) -> list[str]:
    """Перенести текст по внутренней ширине рамки с висячим отступом под префиксом."""
    pw = vlen(prefix)
    lines = wrap(text, inner() - pw)
    return [(prefix if i == 0 else " " * pw) + (style(ln) if style else ln) for i, ln in enumerate(lines)]


def rule(label: str = "") -> None:
    w = width() - 4
    if label:
        lab = f" {label} "
        print("  " + grey("──") + grey(lab) + grey("─" * max(0, w - 2 - vlen(lab))))
    else:
        print("  " + grey("─" * w))


class Spinner:
    """Строка состояния, которая крутится, пока идёт работа в другом потоке."""

    def __init__(self, title: str):
        self.title, self.t0, self.i = title, time.time(), 0

    def draw(self) -> None:
        if not _COLOR:
            return
        self.i += 1
        secs = int(time.time() - self.t0)
        sys.stdout.write(f"\r\033[K  {green(SPIN[self.i % len(SPIN)])} {self.title}{grey('…')} {grey(f'{secs} с')}"
                         f"  {grey(italic('Ctrl+C: стоп'))}")
        sys.stdout.flush()

    @staticmethod
    def clear() -> None:
        if _COLOR:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()


def in_background(fn, title: str):
    """Выполнить fn в потоке, показывая спиннер; Ctrl+C пробрасывается наружу."""
    box: dict = {}

    def work():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()
    sp = Spinner(title)
    while th.is_alive():
        sp.draw()
        th.join(0.1)
    Spinner.clear()
    if "error" in box:
        raise box["error"]
    return box.get("value")


PKG = {"google": "google-genai", "fitz": "pymupdf", "docx": "python-docx", "dotenv": "python-dotenv",
       "multipart": "python-multipart", "yaml": "pyyaml"}


def human_error(e: BaseException) -> tuple[str, str]:
    """Ошибка по-русски и что сделать: без английских сообщений библиотек и трассировок."""
    if isinstance(e, ModuleNotFoundError):
        mod = (e.name or str(e)).split(".")[0]
        return (f"Не установлен пакет {PKG.get(mod, mod)}.",
                "Запустите агента из окружения проекта: .venv\\Scripts\\activate (Windows) или source .venv/bin/activate, "
                "затем pip install -r requirements.txt.")
    if isinstance(e, PermissionError):
        return (f"Нет доступа к файлу: {e.filename or e}.", "Закройте файл в других программах или выберите другую папку.")
    if isinstance(e, FileNotFoundError):
        return (f"Файл не найден: {e.filename or e}.", "Проверьте путь.")
    if isinstance(e, (ConnectionError, TimeoutError)):
        return ("Нет связи с сервисом модели.", "Проверьте интернет или прокси и повторите; готовые шаги сохранены в кэше.")
    if isinstance(e, MemoryError):
        return ("Не хватило памяти.", "Закройте другие программы или проверьте документ меньшего размера.")
    if type(e).__name__ in ("LLMError", "RedactionLeak", "DocumentError", "StandardError", "EditError", "NotFound"):
        return (str(e), "")  # сообщения агента уже на русском
    if isinstance(e, ValueError) and re.search(r"[А-Яа-яЁё]", str(e)):
        return (str(e), "")
    if isinstance(e, (IndexError, KeyError)):
        return ("Нет такого элемента.", "Проверьте номер или ID: /history, /findings, /questions.")
    return (f"Внутренняя ошибка ({type(e).__name__}).",
            "Повторите действие; если ошибка повторяется, запустите python main.py --help или проверку из командной строки.")


def _where(a) -> str:
    return a.where().split(", раздел")[0]


def _first_sentence(s: str) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", (s or "").strip())
    return m.group(1) if m and len(m.group(1)) > 25 else (s or "")


# ---------- сессия ----------


class Shell:
    def __init__(self):
        from .config import Config

        self.cfg = Config.load()
        self.res = None  # последняя открытая проверка
        self.report_path: Path | None = None
        self.history: list[dict] = []  # диалог с помощником по текущей проверке
        self.stop = threading.Event()
        self._runs: list[dict] = []

    # --- вход ---

    def banner(self) -> None:
        from . import __version__
        from .criteria import profile

        from .config import PROVIDERS

        key = green("найден") if self.cfg.api_key else amber(f"нет: введите /key ({PROVIDERS[self.cfg.provider]['key']})")
        fb = f" {grey(f'(запасных: {len(self.cfg.fallbacks)})')}" if self.cfg.fallbacks else ""
        rows = [
            green("▌") + " " + bold("Агент проверки требований") + grey("  статическое ревью документа до разработки"),
            "",
            f"  {grey('модель    ')}{self.cfg.provider} {cyan(self.cfg.model)}{fb}",
            f"  {grey('ключ      ')}{key}",
            f"  {grey('стандарты ')}{profile(self.cfg.standards).titles()}",
            f"  {grey('отчёты    ')}{Path('reports').resolve()}",
        ]
        print()
        panel(rows, color=green, right=grey(f"v{__version__}"))
        print()
        say(f"{bold('Перетащите документ')} в это окно или введите путь (PDF, DOCX, MD, TXT, HTML).")
        say(grey("После проверки спрашивайте о результате обычными словами.  ") + gold("/help") + grey(" команды  ")
            + gold("/exit") + grey(" выход"))
        print()

    def context(self) -> str:
        """Строка над вводом: папка, открытый документ, модель."""
        m = self.cfg.model
        known = {"gemini": ("gemini", "gemma"), "anthropic": ("claude",), "openai": ("gpt", "o1", "o3", "o4")}
        model = m if m.lower().startswith(known.get(self.cfg.provider, ())) else f"{self.cfg.provider} {m}"
        if not self.res:
            doc = "документ не открыт"
        else:
            op = self.res.open_findings()
            crit = sum(f.severity == "critical" for f in op)
            doc = f"{clip(self.res.doc.path.name, 32)}: {len(op)} нарушений, {crit} крит."
        room = width() - 12 - len(doc) - len(model)
        return f"{short_path(Path.cwd(), max(16, room))} / {doc} / {model}"

    def loop(self) -> int:
        self.banner()
        read = _make_reader(self) or (lambda: input("  " + green("› ") if _COLOR else "  > "))
        while True:
            rule(self.context())
            try:
                line = read().strip()
            except EOFError:
                print()
                return 0
            except KeyboardInterrupt:
                print()
                continue
            if not line:
                continue
            try:
                if self.dispatch(line) == "exit":
                    say(grey("До встречи."))
                    return 0
            except KeyboardInterrupt:
                Spinner.clear()
                say("Прервано.", style=amber)
            except Exception as e:  # noqa: BLE001
                Spinner.clear()
                msg, todo = human_error(e)
                hang(red("● "), f"Ошибка: {msg}", style=red)
                if todo:
                    hang("  ", todo, style=grey)
            print()

    def dispatch(self, line: str):
        path = _as_path(line)  # раньше команд: в Linux и macOS путь тоже начинается с «/»
        if path:
            return self.check(str(path))
        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            fn = COMMANDS.get(cmd.lower())
            if not fn:
                say(f"Нет команды /{cmd}. Список: /help", style=amber)
                return None
            return fn(self, arg.strip())
        path = _as_path(line)
        if path:
            return self.check(str(path))
        m = ID_RE.match(line)
        if m and self.res:
            return self.show_id(m.group(1).upper(), int(m.group(2)))
        if not self.res:
            say("Сначала проверьте документ: перетащите файл в окно или введите путь, например "
                + cyan("samples/requirements.pdf") + ". Прошлые проверки: " + gold("/history") + ".")
            return None
        return self.ask(line)

    # --- проверка ---

    def check(self, path: str, fresh: bool = False, dry: bool = False):
        from . import pipeline, report
        from .loader import DocumentError

        cfg = self.cfg
        if fresh:
            import dataclasses

            cfg = dataclasses.replace(cfg, cache_dir=None)
        if not cfg.api_key and not dry:
            say("Нет ключа модели: впишите GEMINI_API_KEY в .env (см. README). Без ключа доступно /dry.", style=red)
            return None
        events: queue.Queue = queue.Queue()
        self.stop.clear()
        box: dict = {}

        def work():
            try:
                box["res"] = pipeline.run(path, cfg, log=events.put, dry_run=dry, should_stop=self.stop.is_set)
            except BaseException as e:  # noqa: BLE001
                box["error"] = e

        th = threading.Thread(target=work, daemon=True)
        th.start()
        sp: Spinner | None = None
        t_all = time.time()
        print()
        try:
            while th.is_alive() or not events.empty():
                try:
                    msg = str(events.get(timeout=0.1))
                except queue.Empty:
                    if sp:
                        sp.draw()
                    continue
                Spinner.clear()
                text = msg.strip()
                if text.startswith("▸"):
                    sp = Spinner(text.lstrip("▸ ").rstrip("…"))
                elif text.startswith("готово за") and sp:
                    done = re.sub(r"^готово за \S+ с:?\s*", "", text)
                    secs = f"{int(time.time() - sp.t0)} с"
                    head = f"{green('●')} {sp.title}"
                    print("  " + head + " " * max(1, width() - 8 - vlen(head) - len(secs)) + grey(secs))
                    if done:
                        hang(grey("⎿ "), done, indent=4, style=grey)
                    sp = None
                elif text.startswith(("Документ:", "Стандарты:", "Маскирование:", "Модель:")):
                    k, _, v = text.partition(":")
                    hang(grey(f"{k.lower():<13}"), v.strip(), indent=4, style=grey)
                elif text.startswith("пропущено") or "повтор" in text or "ответ 4" in text or "ответ 5" in text \
                        or "переключаюсь" in text or "молчит" in text:
                    hang(amber("⎿ "), text, indent=4, style=amber)
                elif text:
                    hang(grey("⎿ "), text, indent=4, style=grey)
        except KeyboardInterrupt:
            Spinner.clear()
            self.stop.set()
            say("Останавливаю после текущего шага: готовые шаги сохранены, повтор продолжит с места…", style=amber)
            th.join()
        Spinner.clear()
        if "error" in box:
            e = box["error"]
            if isinstance(e, DocumentError):
                rows = boxed(e.problem, style=None)
                if e.advice:
                    rows += [""] + boxed(e.advice, prefix=green("Что сделать  "))
                print()
                panel(rows, title=red("Документ нельзя проверить"), color=red)
                return None
            if type(e).__name__ == "Cancelled":
                say("Проверка остановлена.", style=amber)
                return None
            raise e
        res = box["res"]
        if dry:
            return None  # конвейер сам написал, где лежит маскированный документ
        out = Path("reports") / f"{Path(path).stem}_report.md"
        report.write(res, out)
        extras = report.write_extras(res, out)
        self.open_result(res, out)
        print()
        self.summary(seconds=int(time.time() - t_all))
        print()
        hang(grey("отчёт      "), cyan(str(out)))
        if extras:
            hang(grey("вопросы    "), cyan(str(next(iter(extras.values())))))
        print()
        say(grey("Дальше: спросите словами («почему нельзя начинать разработку?», «перепиши Ф-2»), введите ID ")
            + cyan("Н-01") + grey(" или ") + gold("/findings") + grey(", ") + gold("/questions") + grey(", ")
            + gold("/open") + grey("."))
        return None

    def open_result(self, res, path: Path | None) -> None:
        self.res, self.report_path, self.history = res, path, []

    # --- показ результата ---

    def summary(self, seconds: int | None = None) -> None:
        r = self.res
        op = r.open_findings()
        n = {k: sum(1 for f in op if f.severity == k) for k in SEV}

        def verdict(label: str, value: str) -> list[str]:
            bad = value.startswith(("НЕЛЬЗЯ", "ГОТОВНОСТЬ"))
            mark = red("●") if bad else (amber("●") if "ОГОВОРК" in value else green("●"))
            col = red if bad else (amber if "ОГОВОРК" in value else green)
            return boxed(value, prefix=f"{grey(label)}{mark} ", style=lambda s: col(bold(s)))

        rows = verdict("Разработка    ", r.decision_dev) + verdict("Тестирование  ", r.decision_test)
        rows.append("")
        rows.append(f"{bold(str(len(r.requirements)))} {grey('требований')}   {bold(str(len(op)))} {grey('нарушений')}   "
                    f"{bold(str(len(r.questions)))} {grey('вопросов заказчику')}")
        total = max(1, len(op))
        bar_w = max(10, inner() - 34)
        parts = [(n["critical"], red), (n["major"], amber), (n["minor"], grey)]
        cells = [max(1, round(bar_w * k / total)) if k else 0 for k, _ in parts]
        cells[cells.index(max(cells))] += bar_w - sum(cells)
        bar = "".join(c("█" * w) for (k, c), w in zip(parts, cells))
        rows.append(f"{bar}  {red(str(n['critical']) + ' крит')} {grey('/')} {amber(str(n['major']) + ' сущ')} "
                    f"{grey('/')} {grey(str(n['minor']) + ' незн')}")
        right = grey(f"{seconds} с") if seconds is not None else ""
        panel(rows, title="Итог", color=green, right=right)
        crit = [f for f in op if f.severity == "critical"]
        if crit:
            print()
            say(bold("Критичные дефекты"))
            for f in crit:
                self._finding_line(f)
        if r.conclusion and r.conclusion.fix_first:
            print()
            say(bold("Что исправить в первую очередь"))
            for i, it in enumerate(r.conclusion.fix_first[:5], 1):
                hang(gold(f"{i}. "), f"{it.title.rstrip('.')}. " + grey(", ".join(it.finding_ids)), indent=4)

    def _finding_line(self, f) -> None:
        r = self.res
        label, badge, _ = SEV[f.severity]
        ids = ", ".join(dict.fromkeys(r.req(x).doc_id for x in f.req_ids if r.req(x))) or "документ"
        lines = ", ".join(map(str, sorted({a.line for a in f.anchors})[:3]))
        prefix = f"{badge(' ' + label + ' ')} {bold(f.fid)}  "
        text = f"{ids} {grey('/ стр. ' + lines)}  {_first_sentence(f.problem)}"
        print("    " + prefix + clip_ansi(text, width() - 6 - vlen(prefix)))

    def show_id(self, kind: str, num: int):
        r = self.res
        if kind == "Н":
            f = next((x for x in r.findings if x.fid == f"Н-{num:02d}"), None)
            if not f:
                return say(f"Нет находки Н-{num:02d}.", style=amber)
            label, badge, col = SEV[f.severity]
            reqs = ", ".join(f"{x} {r.req(x).doc_id}" for x in f.req_ids if r.req(x)) or "документ целиком"
            rows = boxed(md(f.problem)) + [""]
            for a in f.anchors:
                rows += boxed(f"«{clip(a.text.replace('**', ''), 260)}»  {_where(a)}", prefix=grey("│ "), style=grey)
            rows += [""] + boxed(md(f.fix), prefix=green(bold("Исправить  ")))
            if f.verifier_note:
                rows += [""] + boxed(f.verifier_note, prefix=grey("Верификатор  "), style=grey)
            status = ""
            if f.duplicate_of:
                status = amber(f"дубль {f.duplicate_of}")
            elif not f.kept:
                status = red("отклонена верификатором")
            title = f"{badge(' ' + label.strip() + ' ')} {bold(f.fid)} {grey(r.prof.name(f.criterion) + ' / ' + reqs)}"
            print()
            panel(rows, title=title, color=col, right=status)
        elif kind == "В":
            if not 1 <= num <= len(r.questions):
                return say(f"Нет вопроса В-{num}.", style=amber)
            q = r.questions[num - 1]
            plabel, pbadge = PRIO.get(q.priority, ("?", B_MIN))
            rows = boxed(md(q.question), style=bold) + [""]
            for i, o in enumerate(q.options, 1):
                rows += boxed(o, prefix=gold(f"({i}) "))
            if q.why:
                rows += [""] + boxed(q.why, prefix=grey("Зачем  "), style=grey)
            print()
            panel(rows, title=f"{pbadge(' ' + plabel.strip() + ' ')} {bold(f'В-{num}')} {grey(q.addressee)}",
                  color=gold, right=grey(", ".join(q.finding_ids)))
        else:
            q = r.req(f"ТР-{num:02d}")
            if not q:
                return say(f"Нет требования ТР-{num:02d}.", style=amber)
            rows = boxed(f"«{clip(q.quote.replace('**', ''), 500)}»", prefix=grey("│ "), style=grey)
            own = [f for f in r.open_findings() if q.rid in f.req_ids]
            print()
            panel(rows, title=f"{bold(q.rid)} {q.doc_id}", color=cyan, right=grey(_where(q.anchor)))
            for f in own:
                self._finding_line(f)
            if not own:
                hang(B_OK(" OK ") + " ", "Нарушений нет.", indent=4)
        return None

    # --- помощник ---

    def ask(self, question: str):
        from web import services

        o = services.default_options(self.cfg.provider)
        o.model = self.cfg.model
        out = in_background(lambda: services.assistant(self.res, question, self.history, None, o), "Думаю")
        self.history += [{"role": "user", "content": question}, {"role": "assistant", "content": out["answer"]}]
        print()
        hang(green("● "), md(out["answer"]))
        if out.get("suggested_text"):
            print()
            for ln in wrap(out["suggested_text"], width() - 10):
                print("    " + green("│ ") + ln)
        for e in out.get("edits", []):
            print()
            say(grey(f"Правка, строка {e['line']}" + (f": {e['reason']}" if e.get("reason") else "")), indent=4)
            if e["old_text"].strip():
                hang(red("- "), clip(e["old_text"], 400), indent=4, style=red)
            hang(green("+ "), clip(e["new_text"], 600), indent=4, style=green)
        for a in out.get("answers", []):
            hang(gold(f"{a['question_id']}  "), a["answer"] + (grey(f"  ({a['basis']})") if a.get("basis") else ""), indent=4)
        say(grey(italic(out.get("model", ""))), indent=4)
        return None


def clip_ansi(s: str, n: int) -> str:
    """Обрезать строку с цветами по видимой длине."""
    if vlen(s) <= n:
        return s
    out, seen = [], 0
    for tok in re.split(r"(\033\[[0-9;]*m)", s):
        if ANSI.fullmatch(tok or "x"):
            out.append(tok)
            continue
        if seen + len(tok) > n - 1:
            out.append(tok[: max(0, n - 1 - seen)].rstrip(" ,;:."))
            out.append("…")
            break
        out.append(tok)
        seen += len(tok)
    return "".join(out) + ("\033[0m" if _COLOR else "")


# ---------- команды ----------


def _help(sh: Shell, arg: str):
    groups = [
        ("Проверка", [
            ("путь к файлу", "проверить документ (файл можно перетащить в окно)"),
            ("/check путь [--fresh]", "проверить; --fresh: без кэша ответов"),
            ("/dry путь", "что ушло бы в модель после маскирования, без запросов"),
        ]),
        ("Результат", [
            ("вопрос словами", "спросить помощника о результате"),
            ("Н-03  В-2  ТР-05", "находка, вопрос заказчику, требование"),
            ("/findings [крит|сущ|незн]", "список нарушений"),
            ("/questions", "вопросы заказчику"),
            ("/summary", "итог ещё раз"),
            ("/report  /open", "путь к отчёту, открыть отчёт"),
        ]),
        ("Сессия", [
            ("/history  /load N", "прошлые проверки, открыть одну из них"),
            ("/provider [claude|gemini|openai]", "провайдер модели, без запросов к ней"),
            ("/key", "ввести ключ скрыто; хранится в .env, в модель не уходит"),
            ("/baseurl URL", "свой сервер (Ollama, vLLM, OpenRouter)"),
            ("/model [N|имя]", "выбрать модель из списка (Tab подсказывает)"),
            ("/models", "загрузить список моделей с ключа или сервера"),
            ("/standards [ieee830,ears]", "показать или выбрать стандарты"),
            ("/clear  /exit", "очистить экран, выйти (Ctrl+D)"),
        ]),
    ]
    w = max(len(a) for _, rows in groups for a, _ in rows)
    for name, rows in groups:
        print()
        say(bold(name))
        for a, b in rows:
            cmd = re.sub(r"(/\w+)", lambda m: gold(m.group(1)), a)
            print("    " + cmd + " " * (w - len(a) + 2) + grey(b))
    print()
    say(grey("Ctrl+C во время проверки останавливает её после текущего шага; готовые шаги сохраняются."))


def _check(sh: Shell, arg: str):
    parts = shlex.split(arg, posix=False) if arg else []
    fresh = "--fresh" in parts
    path = _as_path(" ".join(p for p in parts if p != "--fresh"))
    if not path:
        return say("Укажите существующий файл: /check samples/requirements.pdf", style=amber)
    return sh.check(str(path), fresh=fresh)


def _dry(sh: Shell, arg: str):
    path = _as_path(arg)
    if not path:
        return say("Укажите существующий файл: /dry samples/requirements_with_pii.md", style=amber)
    return sh.check(str(path), dry=True)


def _need(sh: Shell) -> bool:
    if not sh.res:
        say("Сначала проверьте документ или откройте прошлую проверку (/history).", style=amber)
        return False
    return True


def _findings(sh: Shell, arg: str):
    if not _need(sh):
        return
    sev = SEV_ARG.get(arg.lower().rstrip("."))
    fs = [f for f in sh.res.open_findings() if not sev or f.severity == sev]
    print()
    for f in sorted(fs, key=lambda f: ("critical", "major", "minor").index(f.severity)):
        sh._finding_line(f)
    print()
    say(grey(f"Всего {len(fs)}. Подробно: введите ID, например ") + cyan(fs[0].fid if fs else "Н-01") + grey("."))


def _questions(sh: Shell, arg: str):
    if not _need(sh):
        return
    print()
    for i, q in enumerate(sh.res.questions, 1):
        label, badge = PRIO.get(q.priority, ("?", B_MIN))
        prefix = f"{badge(' ' + label + ' ')} {bold(f'В-{i}'):<5}  "
        print("    " + prefix + clip(q.question, width() - 8 - vlen(prefix)))
    print()
    say(grey("Варианты ответа: введите ID, например ") + cyan("В-1") + grey(". Ответы заказчика впишите в файл *_questions.md "
        "и повторите проверку с --answers."))


def _summary(sh: Shell, arg: str):
    if _need(sh):
        print()
        sh.summary()


def _report(sh: Shell, arg: str):
    if _need(sh):
        hang(grey("отчёт  "), cyan(str(sh.report_path)))


def _open(sh: Shell, arg: str):
    if not _need(sh) or not sh.report_path:
        return
    p = str(Path(sh.report_path).resolve())
    if os.name == "nt":
        os.startfile(p)  # noqa: S606
    else:
        import subprocess

        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])
    say(grey(f"Открываю {p}"))


def _history(sh: Shell, arg: str):
    from web import services

    runs = services.list_runs()[:15]
    sh._runs = runs
    if not runs:
        return say(grey("Проверок пока нет."))
    print()
    for i, r in enumerate(runs, 1):
        d = r["id"]
        when = f"{d[6:8]}.{d[4:6]} {d[9:11]}:{d[11:13]}" if len(d) >= 13 else d
        print(f"    {gold(str(i).rjust(2))}  {grey(when)}  {clip(r.get('title') or r.get('document', ''), width() - 24)}")
    print()
    say(grey("Открыть: ") + gold("/load N"))


def _load(sh: Shell, arg: str):
    from web import services

    runs = sh._runs or services.list_runs()[:15]
    run_id = runs[int(arg) - 1]["id"] if arg.isdigit() and 0 < int(arg) <= len(runs) else arg
    if not run_id:
        return say("Укажите номер из /history.", style=amber)
    res = services.load_run(run_id)
    files = services.run_files(res)
    sh.open_result(res, files.get("report"))
    print()
    hang(green("● "), f"Открыта проверка {grey(run_id)}: {res.doc.title}")
    print()
    sh.summary()


# Готовые модели по провайдерам, чтобы не набирать имена вручную. Claude: актуальная таблица моделей Anthropic.
MODEL_CATALOG = {
    "anthropic": [
        ("claude-opus-5-5", "по умолчанию: точнее всего на длинных документах"),
        ("claude-sonnet-5-5", "быстрее и в 2 раза дешевле Opus"),
        ("claude-haiku-5-5", "самый быстрый и дешёвый"),
        ("claude-fable-5-2", "самый сильный, дороже Opus"),
        ("claude-opus-5", "прошлое поколение Opus"),
        ("claude-sonnet-5", "прошлое поколение Sonnet"),
        ("claude-opus-4-8", "Opus 4.8"),
        ("claude-haiku-4-5", "Haiku 4.5"),
    ],
    "gemini": [
        ("gemini-3.6-flash", "по умолчанию, бесплатный тариф"),
        ("gemini-3.8-flash", "бесплатный тариф, 20 запросов в сутки"),
        ("gemini-4-hydrogen", "бесплатный тариф"),
        ("gemini-3.5-flash-lite", "быстрый, слабее"),
        ("gemma-4-31b-it", "открытая модель"),
    ],
    "openai": [
        ("gpt-5-mini", "по умолчанию"),
        ("gpt-5", "сильнее и дороже"),
        ("gpt-5-nano", "самый дешёвый"),
        ("qwen3:8b", "Ollama, локально: /baseurl http://localhost:11434/v1"),
        ("llama3.1:8b", "Ollama, локально"),
    ],
}


def _models_for(sh: Shell) -> list[tuple[str, str]]:
    """Готовый список провайдера и, если загружен через /models, реальный список с ключа или сервера."""
    items = list(MODEL_CATALOG.get(sh.cfg.provider, []))
    known = {m for m, _ in items}
    items += [(m, "с вашего ключа") for m in getattr(sh, "_live_models", {}).get(sh.cfg.provider, []) if m not in known]
    return items


def _model(sh: Shell, arg: str):
    from .config import PROVIDERS, save_env

    items = _models_for(sh)
    if arg:
        name = items[int(arg) - 1][0] if arg.isdigit() and 0 < int(arg) <= len(items) else arg.strip()
        sh.cfg.model = name
        save_env(f"{PROVIDERS[sh.cfg.provider]['prefix']}_MODEL", name)
        hang(green("● "), f"Модель {cyan(name)} ({sh.cfg.provider}), сохранена в .env. Модель не вызывалась.")
        return
    print()
    for i, (m, meta) in enumerate(items, 1):
        mark = green("●") if m == sh.cfg.model else " "
        print(f"    {gold(str(i).rjust(2))} {mark} {cyan(m) if m == sh.cfg.model else m}  {grey(meta)}")
    print()
    say(grey("Выбрать: ") + gold("/model N") + grey(" или ") + gold("/model имя") + grey(" (Tab подсказывает). Полный список с ключа: ")
        + gold("/models") + grey(". Запасные: ") + (", ".join(sh.cfg.fallbacks) or "нет"))


def _models(sh: Shell, arg: str):
    """Список моделей, доступных ключу или серверу: только имена, документ не отправляется."""
    from .llm import make_backend

    backend = make_backend(sh.cfg.provider, sh.cfg.api_key, sh.cfg.base_url)
    names = in_background(lambda: sorted(set(backend.list_models())), "Загружаю список моделей")
    names = [n.split("/", 1)[1] if n.startswith("models/") else n for n in names]
    if not hasattr(sh, "_live_models"):
        sh._live_models = {}
    sh._live_models[sh.cfg.provider] = names
    hang(green("● "), f"Доступно моделей: {len(names)}. Они добавлены в подсказки /model (Tab).")
    _model(sh, "")


def _standards(sh: Shell, arg: str):
    from .criteria import available, profile

    if arg:
        ids = [s.strip() for s in arg.split(",") if s.strip()]
        profile(ids)  # неизвестный стандарт: ошибка до проверки
        sh.cfg.standards = ids
    hang(grey("стандарты  "), profile(sh.cfg.standards).titles())
    hang(grey("доступны   "), ", ".join(sorted(available())), style=grey)


def _provider(sh: Shell, arg: str):
    """Сменить провайдера без запросов к модели: gemini, anthropic (Claude), openai (и совместимые)."""
    from .config import PROVIDERS, Config, save_env

    if arg:
        name = arg.strip().lower()
        alias = {"claude": "anthropic", "google": "gemini", "gpt": "openai", "ollama": "openai", "openrouter": "openai"}
        name = alias.get(name, name)
        if name not in PROVIDERS:
            return say(f"Нет провайдера {arg}. Доступны: {', '.join(PROVIDERS)} (claude = anthropic).", style=amber)
        save_env("LLM_PROVIDER", name)
        sh.cfg = Config.load(provider=name)
    p = PROVIDERS[sh.cfg.provider]
    hang(grey("провайдер  "), f"{p['title']} ({sh.cfg.provider})")
    hang(grey("модель     "), cyan(sh.cfg.model))
    hang(grey("ключ       "), green("задан") if sh.cfg.api_key else amber(f"нет: введите /key ({p['key']})"))
    if sh.cfg.provider == "openai":
        hang(grey("адрес      "), sh.cfg.base_url or grey("api.openai.com (свой сервер: /baseurl URL)"))
    say(grey("Модель не вызывалась: настройки сохранены в .env."))


def _key(sh: Shell, arg: str):
    """Ввести ключ скрыто. Ключ сохраняется в .env и окружении процесса; в модель, журналы и историю не попадает."""
    import getpass

    from .config import PROVIDERS, Config, save_env

    if arg.strip():
        say("Не вводите ключ в строке команды: введите просто /key, и агент спросит его скрыто.", style=amber)
        return None
    name = PROVIDERS[sh.cfg.provider]["key"]
    try:
        value = getpass.getpass(f"  {name} (ввод скрыт, Enter: отмена): ").strip()
    except (EOFError, KeyboardInterrupt):
        value = ""
    if not value:
        return say(grey("Ключ не изменён."))
    save_env(name, value)
    sh.cfg = Config.load(provider=sh.cfg.provider)
    hang(green("● "), f"{name} сохранён в .env ({value[:4]}…{value[-2:]}, {len(value)} символов). Модель не вызывалась.")
    return None


def _baseurl(sh: Shell, arg: str):
    """Адрес OpenAI-совместимого сервера: OpenRouter, DeepSeek, Groq, vLLM или Ollama в контуре банка."""
    from .config import Config, save_env

    if not arg:
        return say("Пример: /baseurl http://localhost:11434/v1 (Ollama) или https://openrouter.ai/api/v1", style=amber)
    save_env("OPENAI_BASE_URL", arg.strip())
    save_env("LLM_PROVIDER", "openai")
    sh.cfg = Config.load(provider="openai", base_url=arg.strip())
    hang(green("● "), f"Провайдер openai, адрес {arg.strip()}. Модель: /model ИМЯ, ключ: /key (для Ollama не нужен).")


def _clear(sh: Shell, arg: str):
    os.system("cls" if os.name == "nt" else "clear")
    sh.banner()


def _exit(sh: Shell, arg: str):
    return "exit"


COMMANDS = {"help": _help, "?": _help, "check": _check, "dry": _dry, "findings": _findings, "f": _findings,
            "questions": _questions, "q": _questions, "summary": _summary, "report": _report, "open": _open,
            "history": _history, "load": _load, "model": _model, "standards": _standards, "clear": _clear,
            "provider": _provider, "key": _key, "baseurl": _baseurl, "models": _models,
            "exit": _exit, "quit": _exit}


# ---------- подсказки и автодополнение (prompt_toolkit, если установлен) ----------

DOC_EXT = (".pdf", ".docx", ".md", ".markdown", ".txt", ".html", ".htm")
CMD_HELP = {
    "check": "проверить документ", "dry": "что ушло бы в модель, без запросов", "findings": "список нарушений",
    "questions": "вопросы заказчику", "summary": "итог ещё раз", "report": "путь к отчёту", "open": "открыть отчёт",
    "history": "прошлые проверки", "load": "открыть прошлую проверку", "model": "выбрать модель из списка",
    "standards": "выбрать стандарты", "provider": "gemini, anthropic (Claude), openai и совместимые",
    "key": "ввести ключ скрыто (в модель не уходит)", "models": "список моделей с вашего ключа или сервера", "baseurl": "свой сервер: Ollama, vLLM, OpenRouter",
    "clear": "очистить экран", "help": "все команды", "exit": "выход",
}
EXAMPLES = [
    "почему нельзя начинать разработку?",
    "что исправить в первую очередь и почему?",
    "объясни простыми словами самый опасный дефект",
    "перепиши Ф-2 так, чтобы его можно было проверить",
    "разбей Ф-3 на атомарные требования",
    "составь тест-кейсы для Ф-1",
    "предложи ответы на блокирующие вопросы",
    "какие сценарии не покрыты критериями приёмки?",
]
_PATHLIKE = re.compile(r"^(~|\.{1,2}[\\/]|[A-Za-z]:|/[^/\s]*/|[^\s/\\]+[\\/])")


def _make_reader(sh: "Shell", pt_input=None, pt_output=None):
    """Строка ввода с меню подсказок, серыми подсказками и историей. Без prompt_toolkit: обычный input()."""
    if pt_input is None and not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.auto_suggest import AutoSuggest, AutoSuggestFromHistory, Suggestion
        from prompt_toolkit.completion import Completer, Completion, PathCompleter
        from prompt_toolkit.document import Document
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.styles import Style
    except ImportError:
        return None

    paths = PathCompleter(expanduser=True, file_filter=lambda p: os.path.isdir(p) or p.lower().endswith(DOC_EXT))

    def path_completions(text: str):
        sub = Document(text.strip("\"'"), len(text.strip("\"'")))
        yield from paths.get_completions(sub, None)

    def id_items():
        r = sh.res
        if not r:
            return []
        items = [(f.fid, _first_sentence(f.problem), f.severity) for f in r.open_findings()]
        items += [(f"В-{i}", q.question, q.priority) for i, q in enumerate(r.questions, 1)]
        items += [(q.rid, f"{q.doc_id}: {q.summary}", "req") for q in r.requirements]
        return items

    class ShellCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith("/") and " " not in text and not _PATHLIKE.match(text):
                for name, meta in CMD_HELP.items():
                    if name.startswith(text[1:].lower()):
                        yield Completion("/" + name, -len(text), display="/" + name, display_meta=meta)
                return
            if text.startswith("/"):
                cmd, _, arg = text[1:].partition(" ")
                cmd = cmd.lower()
                if cmd in ("check", "dry"):
                    yield from path_completions(arg)
                elif cmd in ("findings", "f"):
                    for k, meta in (("крит", "критичные"), ("сущ", "существенные"), ("незн", "незначительные")):
                        if k.startswith(arg.lower()):
                            yield Completion(k, -len(arg), display_meta=meta)
                elif cmd == "load":
                    runs = sh._runs or []
                    if not runs:
                        from web import services

                        runs = sh._runs = services.list_runs()[:15]
                    for i, r in enumerate(runs, 1):
                        if str(i).startswith(arg):
                            yield Completion(str(i), -len(arg), display_meta=f"{r['id']}  {clip(r.get('title', ''), 40)}")
                elif cmd == "standards":
                    from .criteria import available

                    last = arg.split(",")[-1].strip()
                    for sid, st in sorted(available().items()):
                        if sid.startswith(last):
                            yield Completion(sid, -len(last), display_meta=clip(st.title, 40))
                elif cmd == "provider":
                    for k, meta in (("anthropic", "Claude"), ("gemini", "Google Gemini"), ("openai", "OpenAI и совместимые")):
                        if k.startswith(arg.lower()):
                            yield Completion(k, -len(arg), display_meta=meta)
                elif cmd == "model":
                    seen = set()
                    for m, meta in [*_models_for(sh), *((f, "запасная") for f in sh.cfg.fallbacks)]:
                        if m not in seen and arg.lower() in m.lower():
                            seen.add(m)
                            yield Completion(m, -len(arg), display_meta=meta)
                elif _PATHLIKE.match(text):
                    yield from path_completions(text)
                return
            m = re.match(r"^\s*([НнВв]|ТР|тр|Тр)(-?\d*)$", text)
            if m and sh.res:
                pre = text.strip().upper().replace("-", "")
                for fid, meta, kind in id_items():
                    if fid.upper().replace("-", "").startswith(pre):
                        yield Completion(fid, -len(text), display_meta=clip(meta, 60))
                return
            if _PATHLIKE.match(text) or (text and " " not in text and any(
                    e.name.lower().startswith(text.lower()) for e in os.scandir(".") if not e.name.startswith("."))):
                yield from path_completions(text)
                return
            if sh.res and (not text or len(text) >= 2):
                for q in EXAMPLES:
                    if q.startswith(text.lower()) and q != text:
                        yield Completion(q, -len(text), display_meta="спросить помощника")

    class ShellSuggest(AutoSuggest):
        """Серая подсказка: сначала из истории, затем команды и примеры вопросов. Принять: стрелка вправо."""

        def __init__(self):
            self.hist = AutoSuggestFromHistory()

        def get_suggestion(self, buffer, document):
            s = self.hist.get_suggestion(buffer, document)
            if s and s.text:
                return s
            text = document.text
            if not text:
                return None
            if text.startswith("/") and " " not in text:
                hits = [n for n in CMD_HELP if n.startswith(text[1:].lower())]
                return Suggestion(hits[0][len(text) - 1:]) if len(hits) == 1 else None
            if sh.res and len(text) >= 3:
                for q in EXAMPLES:
                    if q.startswith(text.lower()):
                        return Suggestion(q[len(text):])
            return None

    style = Style.from_dict({
        "prompt": "#00ab75 bold",
        "completion-menu": "bg:#24262d",
        "completion-menu.completion": "bg:#24262d #e6e6e6",
        "completion-menu.completion.current": "bg:#00ab75 #ffffff bold",
        "completion-menu.meta.completion": "bg:#1e2026 #878a96",
        "completion-menu.meta.completion.current": "bg:#008c60 #ffffff",
        "auto-suggestion": "#5f6370",
        "bottom-toolbar": "noreverse bg:default #6b6f7b",
        "bottom-toolbar.key": "noreverse bg:default #dca86e",
    })
    hist_file = Path.home() / ".bcc_validator_history"

    class SafeHistory(FileHistory):
        """История без секретов: команда /key и строки с ключами на диск не пишутся."""

        def store_string(self, string: str) -> None:
            from .redact import _RE_SECRETS

            keys = [v for k, v in os.environ.items() if k.endswith(("_API_KEY", "_TOKEN", "_SECRET")) and len(v) >= 12]
            secret = (string.lstrip().startswith("/key") or any(rx.search(string) for rx in _RE_SECRETS)
                      or any(v in string for v in keys))
            if secret:
                return
            super().store_string(string)

    try:
        history = SafeHistory(str(hist_file))
    except OSError:
        from prompt_toolkit.history import InMemoryHistory

        history = InMemoryHistory()

    def toolbar():
        return HTML("  <key>Tab</key> подсказки  <key>→</key> принять серую подсказку  <key>↑↓</key> история  "
                    "<key>/help</key> команды  <key>Ctrl+D</key> выход")

    session = PromptSession(history=history, completer=ShellCompleter(), auto_suggest=ShellSuggest(),
                            complete_while_typing=True, style=style, bottom_toolbar=toolbar,
                            reserve_space_for_menu=6, input=pt_input, output=pt_output)
    read = lambda: session.prompt([("class:prompt", "  › ")])  # noqa: E731
    read.session = session  # для тестов: доступ к подсказкам
    return read


def _as_path(s: str) -> Path | None:
    """Путь из ввода: перетаскивание файла в окно даёт его в кавычках или с & '…' (PowerShell)."""
    s = s.strip()
    if s.startswith("& "):
        s = s[2:].strip()
    s = s.strip("\"'").strip()
    if not s or len(s) > 500 or "\n" in s:
        return None
    p = Path(os.path.expanduser(s))
    return p if p.is_file() else None


def run() -> int:
    if not sys.stdin.isatty():
        try:
            sys.stdin.reconfigure(encoding="utf-8")  # ввод из конвейера (скрипты, тесты)
        except Exception:  # noqa: BLE001
            pass
    try:
        return Shell().loop()
    except KeyboardInterrupt:
        print()
        return 0
