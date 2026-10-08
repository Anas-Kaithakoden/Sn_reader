#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a small IMAGE-ONLY Arabic test PDF (no selectable text layer).

The result is a real scanned-style PDF: every page is a single raster image
embedded in the page, ``page.get_text()`` returns nothing, and therefore only
OCR can recover the words.  This is what the OCR pipeline and the reader's
overlay tests are exercised against -- the pre-existing ``test_arabic.pdf``
contains real text and cannot test the scanned path.

    python ocr/make_test_pdf.py                       # data/test_pdfs/test_scanned_arabic.pdf
    python ocr/make_test_pdf.py --output my.pdf

Content (verified recoverable by Tesseract at 300 DPI):

    page 1:  ذهب / كتب / بيت / رحمه / طَرَقَ البَابَ   (short phrases)
    page 2:  bare forms incl. عمل / من for ambiguity tests
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pymupdf  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont

__all__ = ["PAGE_LINES", "make_test_pdf", "DEFAULT_OUTPUT"]

#: 300-DPI A4 raster -- exactly what the pipeline renders back during OCR.
IMAGE_W, IMAGE_H = 2480, 3508
PAGE_PT = (595.0, 842.0)            # A4 in PDF points

PAGE_LINES: dict[int, list[str]] = {
    1: [
        "ذهب الولد إلى السوق",             # ذهب  (bare)
        "كتب الطالب الدرس",                 # كتب  (bare)
        "في بيت الله",                      # بيت  (bare)
        "رحمه الله",                        # رحمه (bare)
        "طَرَقَ البَابَ",                   # required vocalized phrase
    ],
    2: [
        "ذهب أحمد بعد الصلاة",              # ذهب  (bare)
        "كتب الطالب درسه",                  # كتب  (bare)
        "عمل العامل عمله في الحقل",         # عمل  (bare, ambiguous)
        "من هؤلاء الطالبين",                # من   (bare, ambiguous)
        "البيت الكبير والجميل",             # بيت  (bare, with ال)
    ],
}

_FONT_CANDIDATES = (
    "arialbd.ttf", "arial.ttf", "tahomabd.ttf", "tahoma.ttf",
    "timesbd.ttf", "times.ttf",
)
_FONT_DIRS = (
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/System/Library/Fonts"),
    Path("/Library/Fonts"),
)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    errors = []
    for name in _FONT_CANDIDATES:
        for d in _FONT_DIRS:
            path = d / name
            if path.is_file():
                try:
                    return ImageFont.truetype(str(path), size)
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
    # last resort: any installed TrueType font Pillow knows about
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()
    raise RuntimeError("no usable font found: " + "; ".join(errors))


def render_page_image(lines: list[str], width: int = IMAGE_W,
                      height: int = IMAGE_H) -> Image.Image:
    """Draw text lines right-aligned on a white page (raqm shapes Arabic)."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    font = _load_font(150)

    margin_right = 220
    top = 260
    gap = 400
    for i, line in enumerate(lines):
        y = top + i * gap
        # Pillow + raqm lays RTL scripts out in visual order already.
        draw.text((width - margin_right, y), line, font=font,
                  fill="black", anchor="ra")
        if y > height - 300:
            break
    return img


def make_test_pdf(output: str | Path | None = None, *,
                  page_lines: dict[int, list[str]] | None = None) -> Path:
    """Create (or recreate) the image-only test PDF and return its path."""
    out = Path(output) if output is not None else DEFAULT_OUTPUT
    out = out.expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    lines_by_page = page_lines or PAGE_LINES

    doc = pymupdf.open()
    for page_no in sorted(lines_by_page):
        page = doc.new_page(width=PAGE_PT[0], height=PAGE_PT[1])
        img = render_page_image(lines_by_page[page_no])
        buf = io.BytesIO()
        # JPEG keeps the file small while staying indistinguishable from a
        # real scan for OCR purposes (quality 92 is visually lossless here).
        img.save(buf, format="JPEG", quality=95)
        page.insert_image(page.rect, stream=buf.getvalue())
    data = doc.tobytes()
    doc.close()

    out.write_bytes(data)
    return out


DEFAULT_OUTPUT = _ROOT / "data" / "test_pdfs" / "test_scanned_arabic.pdf"


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT),
                    help="where to write the PDF")
    args = ap.parse_args(argv)
    path = make_test_pdf(args.output)
    # sanity: it must really be image-only
    doc = pymupdf.open(path)
    has_text = any(len(p.get_text().strip()) >= 10 for p in doc)
    pages = doc.page_count
    doc.close()
    if has_text:
        print("error: generated PDF unexpectedly contains text", file=sys.stderr)
        return 1
    print(f"wrote {path} ({pages} image-only pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
