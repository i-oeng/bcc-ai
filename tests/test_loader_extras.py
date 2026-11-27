"""Формулы и картинки: математические буквы становятся обычными, картинки без текста видны в предупреждении."""

import pymupdf

from validator.loader import load


def test_math_letters_become_plain_but_superscripts_stay(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("Система должна считать 𝑅1 = 15 Ω и 𝑣1 = 12 sin(ωt), площадь 10 м².\n", encoding="utf-8")
    t = load(p).text
    assert "R1 = 15 Ω" in t and "v1 = 12" in t and "м²" in t


def test_raster_images_are_counted(tmp_path):
    p = tmp_path / "img.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Requirement 1. The system shall show the wiring diagram below. " * 2)  # встроенный шрифт без кириллицы
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200), 0)
    pix.clear_with(180)
    page.insert_image(pymupdf.Rect(72, 120, 372, 320), pixmap=pix)
    small = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 32, 32), 0)  # значок: не считается
    small.clear_with(90)
    page.insert_image(pymupdf.Rect(400, 120, 432, 152), pixmap=small)
    doc.save(str(p))
    d = load(p)
    assert d.images == 1 and d.image_pages == [1]
