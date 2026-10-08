# -*- coding: utf-8 -*-
"""Output schema for the OCR dataset: page files, manifest, reading order.

Layout of a generated dataset (default root ``data/ocr/<pdf-name>/``)::

    manifest.json          dataset identity + engine + DPI + SHA-256
    metadata.json          run details, per-page statistics, errors
    ocr_report.json        quality report (whole dataset)
    ocr_report.csv         one row per page
    pages/001.json         word-level OCR for page 1
    pages/002.json         ...

Page file (schema_version 1)::

    {
      "page": 1,
      "image_width": 2480, "image_height": 3508,
      "processing_time": 1.83, "processed_at": "2026-...Z",
      "words": [
        {"text": "ذَهَبَ",              # raw OCR output, untouched
         "text_clean": "ذَهَبَ",        # stage A: OCR Unicode cleanup
         "text_normalized": "ذهب",       # stage B: dictionary lookup key
         "confidence": 74.0,
         "is_arabic": true, "low_confidence": false, "overlap": false,
         "reading_order": 0,
         "bbox": {"x": 2015, "y": 292, "width": 162, "height": 84},
         "block": 1, "paragraph": 1, "line": 1, "word_index": 1}
      ]
    }

Coordinates are pixels of the page rendered at ``manifest.dpi`` -- exactly the
coordinate system ``reader.html`` scales onto the PDF.js page.
"""

from __future__ import annotations

import json
from typing import Iterable

__all__ = [
    "SCHEMA_VERSION",
    "PAGE_DIR",
    "page_filename",
    "page_number_width",
    "make_word_doc",
    "make_page_doc",
    "make_manifest",
    "validate_page_doc",
    "assign_reading_order",
    "detect_overlapping_words",
    "load_json",
]

SCHEMA_VERSION = 1
PAGE_DIR = "pages"
MIN_PAGE_WIDTH = 3          # 001.json, 002.json, ...


# --------------------------------------------------------------------------
# file naming
# --------------------------------------------------------------------------
def page_number_width(page_count: int) -> int:
    """Zero-padding width for page files (at least 3 digits)."""
    return max(MIN_PAGE_WIDTH, len(str(max(1, int(page_count)))))


def page_filename(page: int, page_count: int) -> str:
    return f"{page:0{page_number_width(page_count)}d}.json"


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def make_word_doc(*, text: str, text_clean: str, text_normalized: str,
                   confidence: float, bbox: dict, is_arabic: bool,
                   low_confidence: bool, reading_order: int,
                   overlap: bool = False, block=None, paragraph=None,
                   line=None, word_index=None) -> dict:
    """One word entry as stored in ``pages/NNN.json``."""
    x, y = bbox["x"], bbox["y"]
    w, h = bbox["width"], bbox["height"]
    doc = {
        "text": text,                       # raw OCR result -- never altered
        "text_clean": text_clean,           # stage A: Unicode cleanup
        "text_normalized": text_normalized, # stage B: dictionary lookup key
        "confidence": round(float(confidence), 1),
        "is_arabic": bool(is_arabic),
        "low_confidence": bool(low_confidence),
        "overlap": bool(overlap),
        "reading_order": int(reading_order),
        "bbox": {
            "x": int(round(x)),
            "y": int(round(y)),
            "width": int(round(w)),
            "height": int(round(h)),
        },
    }
    # structural fields from the engine (help reading order / later analysis)
    if block is not None:
        doc["block"] = int(block)
    if paragraph is not None:
        doc["paragraph"] = int(paragraph)
    if line is not None:
        doc["line"] = int(line)
    if word_index is not None:
        doc["word_index"] = int(word_index)
    return doc


def make_page_doc(page: int, image_width: int, image_height: int,
                  words: list[dict], *, processing_time: float = 0.0,
                  processed_at: str = "", meta: dict | None = None) -> dict:
    doc = {
        "page": int(page),
        "image_width": int(image_width),
        "image_height": int(image_height),
        "processing_time": round(float(processing_time), 3),
        "words": words,
    }
    if processed_at:
        doc["processed_at"] = processed_at
    if meta:
        doc["engine_meta"] = meta
    return doc


def make_manifest(*, source_pdf: str, pdf_sha256: str, page_count: int,
                  engine: str, engine_version: str, language: str, dpi: int,
                  created_at: str, **extra) -> dict:
    """``manifest.json`` -- everything a reader needs before touching pages."""
    doc = {
        "schema_version": SCHEMA_VERSION,
        "source_pdf": source_pdf,
        "pdf_sha256": pdf_sha256,
        "page_count": int(page_count),
        "ocr_engine": engine,
        "ocr_engine_version": engine_version,
        "language": language,
        "dpi": int(dpi),
        "created_at": created_at,
        # reader hints
        "pages_dir": f"{PAGE_DIR}/",
        "page_file_pattern": "pages/{page:0" + str(page_number_width(page_count)) + "d}.json",
        "page_number_width": page_number_width(page_count),
    }
    doc.update(extra)
    return doc


# --------------------------------------------------------------------------
# validation (used to decide whether a page can be skipped on resume)
# --------------------------------------------------------------------------
_REQUIRED_WORD_KEYS = ("text", "text_normalized", "confidence", "bbox")


def validate_page_doc(obj, page: int) -> bool:
    """True when ``obj`` is a complete, well-formed page file for ``page``."""
    try:
        if not isinstance(obj, dict):
            return False
        if obj.get("page") != page:
            return False
        if not isinstance(obj.get("image_width"), int) or obj["image_width"] <= 0:
            return False
        if not isinstance(obj.get("image_height"), int) or obj["image_height"] <= 0:
            return False
        words = obj.get("words")
        if not isinstance(words, list):
            return False
        for w in words:
            if not isinstance(w, dict):
                return False
            for key in _REQUIRED_WORD_KEYS:
                if key not in w:
                    return False
            bbox = w["bbox"]
            if not isinstance(bbox, dict):
                return False
            for key in ("x", "y", "width", "height"):
                if not isinstance(bbox.get(key), (int, float)):
                    return False
            if not isinstance(w["confidence"], (int, float)):
                return False
        return True
    except (KeyError, TypeError):
        return False


def load_json(path):
    """Read a JSON file; returns ``None`` when missing or corrupt."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# reading order
# --------------------------------------------------------------------------
def _reading_order_key(word: dict) -> tuple:
    """Page > block > paragraph > line, then right-to-left inside the line.

    Arabic reads right to left, so within one line the word furthest to the
    RIGHT comes first: sort by descending x-centre.  Individual strings are
    never reversed -- only the ORDER of the word list changes.
    """
    bbox = word.get("bbox", {})
    centre_x = bbox.get("x", 0) + bbox.get("width", 0) / 2.0
    return (
        word.get("block") or 0,
        word.get("paragraph") or 0,
        word.get("line") or 0,
        -centre_x,
    )


def assign_reading_order(words: list[dict]) -> list[dict]:
    """Sort ``words`` into deterministic reading order, in place.

    Sets ``reading_order`` (0-based) on every word and returns the sorted
    list.  Sorting uses engine structure first (block/paragraph/line) and
    right-to-left position within a line.
    """
    words.sort(key=_reading_order_key)
    for i, w in enumerate(words):
        w["reading_order"] = i
    return words


# --------------------------------------------------------------------------
# overlap detection (recorded, never deleted)
# --------------------------------------------------------------------------
def _intersection_over_smaller(a: dict, b: dict) -> float:
    ax1, ay1 = a["x"], a["y"]
    ax2, ay2 = ax1 + a["width"], ay1 + a["height"]
    bx1, by1 = b["x"], b["y"]
    bx2, by2 = bx1 + b["width"], by1 + b["height"]

    ix = max(0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    area_a = a["width"] * a["height"]
    area_b = b["width"] * b["height"]
    smaller = min(area_a, area_b)
    return inter / smaller if smaller else 0.0


def detect_overlapping_words(words: list[dict], threshold: float = 0.5) -> int:
    """Flag suspiciously overlapping words; returns how many were flagged.

    Detections are only *marked* (``"overlap": true``) -- nothing is removed.
    Two boxes count as suspicious when they cover at least ``threshold`` of the
    smaller one (duplicated or near-duplicated detections score ~1.0, while
    ordinary side-by-side words score near 0).
    """
    if len(words) < 2:
        return 0

    # Sort by LEFT edge so the scan stops as soon as a word starts at/past
    # the reference word's right edge (later words only start further right).
    order = sorted(range(len(words)), key=lambda i: words[i]["bbox"]["x"])
    for ai in range(len(order)):
        a = words[order[ai]]["bbox"]
        a_right = a["x"] + a["width"]
        for bi in range(ai + 1, len(order)):
            b = words[order[bi]]["bbox"]
            if b["x"] >= a_right:
                break                           # sorted: no further overlaps
            if _intersection_over_smaller(a, b) >= threshold:
                words[order[ai]]["overlap"] = True
                words[order[bi]]["overlap"] = True
    flagged = sum(1 for w in words if w.get("overlap"))
    return flagged
