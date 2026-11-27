"""Повреждённые и нечитаемые документы: понятная диагностика в отчёте, а не аварийное завершение."""

import os
import zipfile

import pymupdf
import pytest

from validator import cli
from validator.loader import DocumentError, load


def _scan(p):
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 50), 0)
    pix.clear_with(200)
    page.insert_image(page.rect, pixmap=pix)
    doc.save(str(p))


def _encrypted(p):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Requirement 1. The system shall work.")
    doc.save(str(p), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")


def _zip_without_word(p):
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "x")


CASES = {
    "garbage.pdf": (lambda p: p.write_bytes(os.urandom(3000)), "не PDF"),
    "empty.pdf": (lambda p: p.write_bytes(b""), "пустой"),
    "blank.md": (lambda p: p.write_text("  \n\n  ", encoding="utf-8"), "пробелы"),
    "binary.txt": (lambda p: p.write_bytes(bytes(range(256)) * 20), "не текстовый"),
    "garbage.docx": (lambda p: p.write_bytes(os.urandom(3000)), "документ Word"),
    "notword.docx": (_zip_without_word, "word/document.xml"),
    "scan.pdf": (_scan, "скан"),
    "locked.pdf": (_encrypted, "паролем"),
    "short.md": (lambda p: p.write_text("Привет", encoding="utf-8"), "слишком мало"),
    "story.md": (lambda p: p.write_text("Сегодня в Алматы солнечно и тепло. " * 20, encoding="utf-8"), "не похоже"),
    "file.xyz": (lambda p: p.write_text("текст", encoding="utf-8"), "не поддерживается"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_broken_document_gives_clear_reason(tmp_path, name):
    make, words = CASES[name]
    p = tmp_path / name
    make(p)
    with pytest.raises(DocumentError, match=words) as e:
        load(p)
    assert e.value.advice  # всегда есть совет, что сделать


def test_missing_file_and_folder(tmp_path):
    with pytest.raises(DocumentError, match="не найден"):
        load(tmp_path / "nope.md")
    with pytest.raises(DocumentError, match="папка"):
        load(tmp_path)


def test_utf16_text_is_read(tmp_path):
    p = tmp_path / "req.txt"
    p.write_bytes("Система должна отправлять уведомление клиенту не позже 5 секунд после покупки.".encode("utf-16"))
    assert "должна" in load(p).text


def test_cli_writes_diagnostic_report_without_model(tmp_path, capsys):
    src = tmp_path / "broken.pdf"
    src.write_bytes(os.urandom(2000))
    out = tmp_path / "report.md"
    code = cli.main([str(src), str(out), "--provider", "gemini"])
    assert code == 2
    text = out.read_text(encoding="utf-8")
    assert "Проверка не выполнена" in text
    assert "## Что не так" in text and "## Что сделать" in text
    assert "Запросы к модели не отправлялись" in text
    assert "Traceback" not in capsys.readouterr().err
