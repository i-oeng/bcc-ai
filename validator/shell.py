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
import textwrap
import threading
import time
from pathlib import Path

# ---------- оформление ----------

_COLOR = sys.stdout.isatty() and not os.getenv("NO_COLOR")
if _COLOR and os.name == "nt":
    os.system("")  # включить ANSI-последовательности в консоли Windows


def _c(code: str):
    return (lambda s: f"\033[{code}m{s}\033[0m") if _COLOR else (lambda s: s)


bold, dim, green, red, yellow, cyan = _c("1"), _c("2"), _c("32"), _c("31"), _c("33"), _c("36")
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SEV = {"critical": ("крит.", red), "major": ("сущ.", yellow), "minor": ("незн.", dim)}
SEV_ARG = {"крит": "critical", "критичные": "critical", "critical": "critical", "сущ": "major", "существенные": "major",
           "major": "major", "незн": "minor", "незначительные": "minor", "minor": "minor"}
ID_RE = re.compile(r"^\s*([НнВв]|ТР|тр)-?(\d+)\s*$")


def width() -> int:
    return max(60, min(shutil.get_terminal_size((100, 30)).columns, 120))


def say(text: str = "", indent: int = 2, style=None) -> None:
    """Абзац с переносом по ширине терминала."""
    pad = " " * indent
    for para in str(text).split("\n"):
        lines = textwrap.wrap(para, width() - indent - 1) or [""]
        for ln in lines:
            print(pad + (style(ln) if style else ln))


def clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n * 0.6 else cut).rstrip(" ,;:.") + "…"


class Spinner:
    """Строка состояния, которая крутится, пока идёт работа в другом потоке."""

    def __init__(self, title: str):
        self.title, self.t0, self.i = title, time.time(), 0

    def draw(self) -> None:
        if not _COLOR:
            return
        self.i += 1
        secs = int(time.time() - self.t0)
        sys.stdout.write(f"\r\033[K  {cyan(SPIN[self.i % len(SPIN)])} {self.title} {dim(f'{secs} с')}")
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


# ---------- сессия ----------


class Shell:
    def __init__(self):
        from .config import Config

        self.cfg = Config.load()
        self.res = None  # последняя открытая проверка
        self.report_path: Path | None = None
        self.history: list[dict] = []  # диалог с помощником по текущей проверке
        self.stop = threading.Event()

    # --- вход ---

    def banner(self) -> None:
        from . import __version__
        from .criteria import profile

        key = green("ключ найден") if self.cfg.api_key else red("нет ключа: впишите его в .env")
        line = "─" * (width() - 4)
        print()
        print("  " + dim("╭" + line + "╮"))
        print("  " + dim("│") + bold("  Агент проверки требований") + dim(f"  {__version__}"))
        print("  " + dim("│") + f"  {self.cfg.provider} {cyan(self.cfg.model)} / {key} / {profile(self.cfg.standards).titles()}")
        print("  " + dim("╰" + line + "╯"))
        say("Перетащите документ в это окно или введите путь (PDF, DOCX, MD, TXT, HTML). "
            "После проверки спрашивайте о результате обычными словами. /help: все команды, /exit: выход.", style=dim)
        print()

    def loop(self) -> int:
        self.banner()
        while True:
            try:
                line = input(green("› ") if _COLOR else "> ").strip()
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
                    return 0
            except KeyboardInterrupt:
                Spinner.clear()
                say("Прервано.", style=yellow)
            except Exception as e:  # noqa: BLE001
                Spinner.clear()
                say(f"Ошибка: {e}", style=red)
            print()

    def dispatch(self, line: str):
        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            fn = COMMANDS.get(cmd.lower())
            if not fn:
                say(f"Нет команды /{cmd}. Список: /help", style=yellow)
                return None
            return fn(self, arg.strip())
        path = _as_path(line)
        if path:
            return self.check(str(path))
        m = ID_RE.match(line)
        if m and self.res:
            return self.show_id(m.group(1).upper(), int(m.group(2)))
        if not self.res:
            say("Сначала проверьте документ: перетащите файл в окно или введите путь. Пример: "
                "samples/requirements.pdf", style=yellow)
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
                    secs = int(time.time() - sp.t0)
                    print(f"  {green('●')} {sp.title} {dim(f'{secs} с')}" + (f"  {dim(clip(done, width() - 40))}" if done else ""))
                    sp = None
                elif text.startswith(("Документ:", "Стандарты:", "Маскирование:", "Модель:")):
                    say(text, style=dim)
                elif text.startswith("пропущено") or "повтор через" in text or "ответ 4" in text or "ответ 5" in text:
                    say(text, indent=4, style=yellow)
                elif text:
                    say(text, indent=4, style=dim)
        except KeyboardInterrupt:
            Spinner.clear()
            self.stop.set()
            say("Останавливаю после текущего шага (готовые шаги сохранены в кэше, повтор продолжит с места)…", style=yellow)
            th.join()
        Spinner.clear()
        if "error" in box:
            e = box["error"]
            if isinstance(e, DocumentError):
                say(f"Документ нельзя проверить: {e.problem}", style=red)
                if e.advice:
                    say(e.advice)
                return None
            if type(e).__name__ == "Cancelled":
                say("Проверка остановлена.", style=yellow)
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
        say(f"Готово за {int(time.time() - t_all)} с.", style=dim)
        self.summary()
        say(f"Отчёт: {out}" + (f"  вопросы заказчику: {next(iter(extras.values()))}" if extras else ""), style=cyan)
        say("Спросите что угодно о результате, например «почему нельзя начинать разработку?» или «перепиши Ф-2». "
            "Или: /findings, /questions, Н-03, /open.", style=dim)
        return None

    def open_result(self, res, path: Path | None) -> None:
        self.res, self.report_path, self.history = res, path, []

    # --- показ результата ---

    def summary(self) -> None:
        r = self.res
        op = r.open_findings()
        n = {k: sum(1 for f in op if f.severity == k) for k in SEV}
        stop_word = lambda s: red(s) if s.startswith(("НЕЛЬЗЯ", "ГОТОВНОСТЬ")) else green(s)  # noqa: E731
        print()
        print(f"  {bold('Разработка   ')} {stop_word(r.decision_dev)}")
        print(f"  {bold('Тестирование ')} {stop_word(r.decision_test)}")
        print(f"  {dim(f'{len(r.requirements)} требований / {len(op)} нарушений')} "
              f"{red(str(n['critical']) + ' крит.')} {yellow(str(n['major']) + ' сущ.')} {dim(str(n['minor']) + ' незн.')} "
              f"{dim(f'/ {len(r.questions)} вопросов заказчику')}")
        crit = [f for f in op if f.severity == "critical"]
        if crit:
            print()
            print("  " + bold("Критичные дефекты"))
            for f in crit:
                self._finding_line(f)
        if r.conclusion and r.conclusion.fix_first:
            print()
            print("  " + bold("Что исправить в первую очередь"))
            for i, it in enumerate(r.conclusion.fix_first[:5], 1):
                say(f"{i}. {it.title.rstrip('.')}. {dim(', '.join(it.finding_ids))}", indent=4)

    def _finding_line(self, f) -> None:
        r = self.res
        label, color = SEV[f.severity]
        ids = ", ".join(dict.fromkeys(r.req(x).doc_id for x in f.req_ids if r.req(x))) or "документ"
        lines = ", ".join(map(str, sorted({a.line for a in f.anchors})[:3]))
        head = f"{f.fid}  {label:<5} "
        rest = f"{ids} / стр. {lines}  {_first_sentence(f.problem)}"
        print("    " + color(head) + clip(rest, width() - len(head) - 6))

    def show_id(self, kind: str, num: int):
        r = self.res
        if kind == "Н":
            f = next((x for x in r.findings if x.fid == f"Н-{num:02d}"), None)
            if not f:
                return say(f"Нет находки Н-{num:02d}.", style=yellow)
            label, color = SEV[f.severity]
            crit = r.prof.name(f.criterion)
            print()
            print(f"  {bold(f.fid)} {color(label)} {dim('/ ' + crit + ' / ' + ', '.join(f.req_ids))}"
                  + (dim(f" / дубль {f.duplicate_of}") if f.duplicate_of else "")
                  + (red(" / отклонена верификатором") if not f.kept else ""))
            say(f.problem)
            for a in f.anchors:
                say(f"«{clip(a.text, 300)}» ({a.where().split(', раздел')[0]})", indent=4, style=dim)
            say(green("Исправить: ") + f.fix)
            if f.verifier_note:
                say(f"Верификатор: {f.verifier_note}", style=dim)
        elif kind == "В":
            if not 1 <= num <= len(r.questions):
                return say(f"Нет вопроса В-{num}.", style=yellow)
            q = r.questions[num - 1]
            print()
            prio = {"blocking": "блокирующий", "important": "важный", "nice": "желательный"}.get(q.priority, q.priority)
            print(f"  {bold(f'В-{num}')} {dim(prio + ' / ' + q.addressee + ' / ' + ', '.join(q.finding_ids))}")
            say(q.question)
            for i, o in enumerate(q.options, 1):
                say(f"({i}) {o}", indent=4)
            if q.why:
                say(f"Почему важно: {q.why}", style=dim)
        else:
            q = r.req(f"ТР-{num:02d}")
            if not q:
                return say(f"Нет требования ТР-{num:02d}.", style=yellow)
            print()
            print(f"  {bold(q.rid)} {dim(q.doc_id + ' / ' + q.anchor.where().split(', раздел')[0])}")
            say(f"«{clip(q.quote, 400)}»", style=dim)
            own = [f for f in r.open_findings() if q.rid in f.req_ids]
            for f in own:
                self._finding_line(f)
            if not own:
                say("Нарушений нет.", style=green)
        return None

    # --- помощник ---

    def ask(self, question: str):
        from web import services

        o = services.default_options(self.cfg.provider)
        o.model = self.cfg.model
        out = in_background(lambda: services.assistant(self.res, question, self.history, None, o), "думаю")
        self.history += [{"role": "user", "content": question}, {"role": "assistant", "content": out["answer"]}]
        print()
        say(out["answer"])
        if out.get("suggested_text"):
            print()
            say(out["suggested_text"], indent=4, style=cyan)
        for e in out.get("edits", []):
            print()
            say(f"Правка, строка {e['line']}" + (f": {e['reason']}" if e.get("reason") else ""), style=dim)
            if e["old_text"].strip():
                say("было:  " + clip(e["old_text"], 400), indent=4, style=red)
            say("стало: " + clip(e["new_text"], 600), indent=4, style=green)
        for a in out.get("answers", []):
            say(f"{a['question_id']}: {a['answer']}" + (f" ({a['basis']})" if a.get("basis") else ""), indent=4)
        say(dim(out.get("model", "")), indent=2)
        return None


# ---------- команды ----------


def _help(sh: Shell, arg: str):
    rows = [
        ("путь к файлу", "проверить документ (можно перетащить файл в окно)"),
        ("вопрос словами", "спросить помощника о результате (маскируется так же, как документ)"),
        ("Н-03, В-2, ТР-05", "показать находку, вопрос заказчику или требование"),
        ("/check путь [--fresh]", "проверить; --fresh: без кэша ответов"),
        ("/dry путь", "сухая проверка: что ушло бы в модель, без запросов"),
        ("/findings [крит|сущ|незн]", "список нарушений"),
        ("/questions", "вопросы заказчику"),
        ("/summary", "итог текущей проверки ещё раз"),
        ("/report, /open", "путь к отчёту / открыть отчёт"),
        ("/history, /load N", "прошлые проверки / открыть одну из них"),
        ("/model [имя]", "показать или сменить модель"),
        ("/standards [ieee830,ears,...]", "показать или выбрать стандарты"),
        ("/clear, /exit", "очистить экран / выйти (или Ctrl+D)"),
    ]
    w = max(len(a) for a, _ in rows)
    print()
    for a, b in rows:
        print(f"  {cyan(a.ljust(w))}  {b}")
    say("Ctrl+C во время проверки останавливает её после текущего шага.", style=dim)


def _check(sh: Shell, arg: str):
    parts = shlex.split(arg, posix=False) if arg else []
    fresh = "--fresh" in parts
    path = _as_path(" ".join(p for p in parts if p != "--fresh"))
    if not path:
        return say("Укажите существующий файл: /check samples/requirements.pdf", style=yellow)
    return sh.check(str(path), fresh=fresh)


def _dry(sh: Shell, arg: str):
    path = _as_path(arg)
    if not path:
        return say("Укажите существующий файл: /dry samples/requirements_with_pii.md", style=yellow)
    return sh.check(str(path), dry=True)


def _need(sh: Shell) -> bool:
    if not sh.res:
        say("Сначала проверьте документ или откройте прошлую проверку (/history).", style=yellow)
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
    say(f"Всего: {len(fs)}. Подробно: введите ID, например {fs[0].fid if fs else 'Н-01'}.", style=dim)


def _questions(sh: Shell, arg: str):
    if not _need(sh):
        return
    names = {"blocking": red("блок."), "important": yellow("важн."), "nice": dim("желат.")}
    print()
    for i, q in enumerate(sh.res.questions, 1):
        print(f"  {bold(f'В-{i}'):<6} {names.get(q.priority, q.priority)}  " + clip(q.question, width() - 22))
    say("Подробно и варианты ответа: введите ID, например В-1. Ответы заказчика: файл *_questions.md "
        "и повторная проверка: python main.py --input ДОКУМЕНТ --output ОТЧЁТ --answers ФАЙЛ.", style=dim)


def _summary(sh: Shell, arg: str):
    if _need(sh):
        sh.summary()


def _report(sh: Shell, arg: str):
    if _need(sh):
        say(f"Отчёт: {sh.report_path}", style=cyan)


def _open(sh: Shell, arg: str):
    if not _need(sh) or not sh.report_path:
        return
    p = str(Path(sh.report_path).resolve())
    if os.name == "nt":
        os.startfile(p)  # noqa: S606
    else:
        import subprocess

        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])
    say(f"Открываю {p}", style=dim)


def _history(sh: Shell, arg: str):
    from web import services

    runs = services.list_runs()[:15]
    sh._runs = runs
    if not runs:
        return say("Проверок пока нет.", style=dim)
    print()
    for i, r in enumerate(runs, 1):
        print(f"  {cyan(str(i).rjust(2))}  {r['id']}  {clip(r.get('title') or r.get('document', ''), width() - 30)}")
    say("Открыть: /load N", style=dim)


def _load(sh: Shell, arg: str):
    from web import services

    runs = getattr(sh, "_runs", None) or services.list_runs()[:15]
    run_id = runs[int(arg) - 1]["id"] if arg.isdigit() and 0 < int(arg) <= len(runs) else arg
    if not run_id:
        return say("Укажите номер из /history.", style=yellow)
    res = services.load_run(run_id)
    files = services.run_files(res)
    sh.open_result(res, files.get("report"))
    say(f"Открыта проверка {run_id}: {res.doc.title}", style=green)
    sh.summary()


def _model(sh: Shell, arg: str):
    if arg:
        sh.cfg.model = arg
    say(f"Модель: {sh.cfg.provider} {sh.cfg.model}; запасные: {', '.join(sh.cfg.fallbacks) or 'нет'}")


def _standards(sh: Shell, arg: str):
    from .criteria import available, profile

    if arg:
        ids = [s.strip() for s in arg.split(",") if s.strip()]
        profile(ids)  # неизвестный стандарт: ошибка до проверки
        sh.cfg.standards = ids
    say(f"Стандарты: {profile(sh.cfg.standards).titles()}")
    say("Доступны: " + ", ".join(sorted(available())), style=dim)


def _clear(sh: Shell, arg: str):
    os.system("cls" if os.name == "nt" else "clear")
    sh.banner()


def _exit(sh: Shell, arg: str):
    return "exit"


COMMANDS = {"help": _help, "?": _help, "check": _check, "dry": _dry, "findings": _findings, "f": _findings,
            "questions": _questions, "q": _questions, "summary": _summary, "report": _report, "open": _open,
            "history": _history, "load": _load, "model": _model, "standards": _standards, "clear": _clear,
            "exit": _exit, "quit": _exit}


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


def _first_sentence(s: str) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", (s or "").strip())
    return m.group(1) if m and len(m.group(1)) > 25 else (s or "")


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
