#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end tests for the OCR preprocessing pipeline and reader overlay.

Run::

    .venv\\Scripts\\python.exe -X utf8 test_ocr_pipeline.py

What is covered (all against REAL components -- no mocks for the reader):

1. scanned-PDF detection          -- the test PDF is truly image-only
2. pipeline output                -- page JSONs: bboxes in bounds, confidence,
                                     normalized keys, RTL reading order,
                                     manifest / reports / CSV correctness
3. normalization stages           -- stage A (OCR Unicode cleanup) and stage B
                                     (dictionary normalization reuse) stay apart
4. dictionary integration         -- lookup semantics: exact then normalized,
                                     ALL candidates, never a single winner
5. resume / identity / failures   -- --limit resume, SHA-256 identity guard,
                                     pages-without-manifest guard, per-page
                                     failure-continue reporting
6. CLI                            -- ocr_pdf.py subprocess run
7. reader end-to-end              -- REAL headless Chrome over CDP:
                                     lazy OCR overlay, transparent word boxes,
                                     click -> dictionary popup, text-PDF path
                                     regression

Fixtures (data/test_pdfs/test_scanned_arabic.pdf + its OCR dataset) are
recreated automatically in setUpModule when missing.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import pymupdf  # PyMuPDF

import database.lookup as dblookup
import headless_chrome
from headless_chrome import Chrome, ChromeError
from ocr.make_test_pdf import make_test_pdf
from ocr.normalize_ocr import cleanup_ocr_text, dictionary_normalize, is_arabic_word
from ocr.ocr_backend import OCRBackend
from ocr.ocr_pdf import (
    PdfIdentityError,
    default_output_dir,
    run_pipeline,
    scan_pdf_content,
)
from ocr.ocr_schema import load_json, validate_page_doc
from ocr.tesseract_backend import TesseractBackend
from normalized_buhaira.normalize_arabic import normalize_arabic

ROOT = Path(__file__).resolve().parent
SCANNED_PDF = ROOT / "data" / "test_pdfs" / "test_scanned_arabic.pdf"
TEXT_PDF = ROOT / "test_arabic.pdf"
DATASET_DIR = default_output_dir(SCANNED_PDF)

HARAKAT_RE = "[\u064B-\u0652\u0670]"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def _ensure_fixtures() -> None:
    """Recreate the image-only test PDF and its OCR dataset when missing."""
    if not SCANNED_PDF.is_file():
        make_test_pdf()
    manifest = DATASET_DIR / "manifest.json"
    pages = DATASET_DIR / "pages"
    if not manifest.is_file() or not pages.is_dir() or not list(pages.glob("*.json")):
        summary = run_pipeline(SCANNED_PDF, DATASET_DIR)
        assert summary["totals"]["failed_pages"] == []


def setUpModule() -> None:
    _ensure_fixtures()


def tearDownModule() -> None:
    dblookup.close()


def _load_dataset():
    with open(DATASET_DIR / "manifest.json", encoding="utf-8") as fh:
        manifest = json.load(fh)
    page_docs = {}
    for page_file in sorted((DATASET_DIR / "pages").glob("*.json")):
        with open(page_file, encoding="utf-8") as fh:
            page_docs[page_file.stem.lstrip("0") or "0"] = json.load(fh)
    return manifest, page_docs


# --------------------------------------------------------------------------
# 1. scanned-PDF detection
# --------------------------------------------------------------------------
class TestScannedPdfDetection(unittest.TestCase):
    def test_pdf_is_truly_image_only(self):
        doc = pymupdf.open(str(SCANNED_PDF))
        try:
            texts = [page.get_text().strip() for page in doc]
        finally:
            doc.close()
        self.assertEqual(len(texts), 2)
        self.assertTrue(all(t == "" for t in texts),
                        "image-only test PDF must contain no selectable text")

    def test_content_scan_classifies_as_scanned(self):
        doc = pymupdf.open(str(SCANNED_PDF))
        try:
            info = scan_pdf_content(doc)
        finally:
            doc.close()
        self.assertEqual(info["kind"], "scanned")
        self.assertEqual(info["pages_with_text"], 0)
        self.assertEqual(info["pages_with_images"], 2)


# --------------------------------------------------------------------------
# 2. pipeline output
# --------------------------------------------------------------------------
class TestPipelineOutput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.pages = _load_dataset()

    def test_manifest_fields(self):
        m = self.manifest
        self.assertEqual(m["schema_version"], 1)
        self.assertEqual(m["page_count"], 2)
        self.assertEqual(m["ocr_engine"], "tesseract")
        self.assertEqual(m["language"], "ara")
        self.assertEqual(m["dpi"], 300)
        self.assertEqual(m["status"], "complete")
        self.assertEqual(m["failed_pages"], [])
        self.assertEqual(m["total_words"], 32)
        self.assertEqual(m["page_number_width"], 3)
        self.assertEqual(m["content"]["kind"], "scanned")
        self.assertEqual(len(m["pdf_sha256"]), 64)
        self.assertTrue(int(m["pdf_sha256"], 16) > 0)

    def test_page_schema_and_bboxes(self):
        for page, doc in self.pages.items():
            self.assertTrue(validate_page_doc(doc, int(page)))
            img_w, img_h = doc["image_width"], doc["image_height"]
            self.assertGreater(img_w, 0)
            self.assertGreater(img_h, 0)
            for w in doc["words"]:
                b = w["bbox"]
                self.assertGreater(b["width"], 0)
                self.assertGreater(b["height"], 0)
                self.assertGreaterEqual(b["x"], 0)
                self.assertGreaterEqual(b["y"], 0)
                self.assertLessEqual(b["x"] + b["width"], img_w,
                                     "word box must be inside the rendered page")
                self.assertLessEqual(b["y"] + b["height"], img_h)

    def test_word_field_completeness(self):
        required = ("text", "text_clean", "text_normalized", "confidence",
                    "is_arabic", "low_confidence", "overlap", "reading_order",
                    "bbox", "block", "paragraph", "line", "word_index")
        for doc in self.pages.values():
            for w in doc["words"]:
                for key in required:
                    self.assertIn(key, w, f"missing {key!r} in {w!r}")

    def test_required_words_recovered(self):
        page1_norm = {w["text_normalized"] for w in self.pages["1"]["words"]}
        self.assertLessEqual({"ذهب", "كتب", "بيت", "رحمه"}, page1_norm)
        # the required vocalized phrase survives OCR (with its harakat)
        self.assertIn("طرق", page1_norm)
        self.assertIn("الباب", page1_norm)
        raw = [w["text"] for w in self.pages["1"]["words"]]
        self.assertTrue(any(re.search(HARAKAT_RE, t) for t in raw),
                        "raw OCR text must preserve harakat")

    def test_raw_text_never_overwritten(self):
        # طَرَقَ البَابَ must keep its diacritics in `text` while the
        # normalized key drops them for dictionary lookup.
        for doc in self.pages.values():
            for w in doc["words"]:
                if "طَرَقَ" in w["text"]:
                    self.assertEqual(w["text"], "طَرَقَ")
                    self.assertEqual(w["text_normalized"], "طرق")
                    self.assertNotEqual(w["text"], w["text_normalized"])

    def test_reading_order_is_rtl_within_lines(self):
        for doc in self.pages.values():
            words = doc["words"]
            orders = [w["reading_order"] for w in words]
            self.assertEqual(orders, list(range(len(words))))
            # deterministic: re-sorting must reproduce the stored order
            by_order = sorted(words, key=lambda w: w["reading_order"])
            self.assertEqual([w["reading_order"] for w in by_order],
                             list(range(len(words))))
            # right-to-left inside each text line
            lines = {}
            for w in words:
                lines.setdefault((w["block"], w["paragraph"], w["line"]), []).append(w)
            for line_words in lines.values():
                centres = [w["bbox"]["x"] + w["bbox"]["width"] / 2 for w in line_words]
                self.assertEqual(centres, sorted(centres, reverse=True),
                                 "words must read right-to-left within a line")

    def test_confidence_and_reports(self):
        with open(DATASET_DIR / "ocr_report.json", encoding="utf-8") as fh:
            report = json.load(fh)
        words = [w for doc in self.pages.values() for w in doc["words"]]
        avg = round(sum(w["confidence"] for w in words) / len(words), 2)
        self.assertAlmostEqual(report["average_confidence"], avg, places=2)
        self.assertGreater(report["average_confidence"], 60.0)
        low = [w for w in words if w["low_confidence"]]
        self.assertEqual(len(low), sum(w["confidence"] < 60 for w in words))
        self.assertGreaterEqual(len(low), 1)   # the vocalized phrase has lower conf
        self.assertEqual(report["failed_pages"], [])
        self.assertEqual(report["page_count"], 2)

    def test_metadata_and_csv(self):
        with open(DATASET_DIR / "metadata.json", encoding="utf-8") as fh:
            metadata = json.load(fh)
        self.assertEqual(metadata["pdf"]["page_count"], 2)
        self.assertEqual(metadata["totals"]["total_words"], 32)
        self.assertEqual(metadata["pipeline"]["engine"], "tesseract")
        with open(DATASET_DIR / "ocr_report.csv", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["page"], "1")
        self.assertEqual(rows[0]["word_count"], "14")
        self.assertEqual(rows[1]["page"], "2")
        self.assertEqual(rows[1]["word_count"], "18")
        self.assertGreater(os.path.getsize(DATASET_DIR / "pages" / "001.json"), 0)


# --------------------------------------------------------------------------
# 3. normalization stages
# --------------------------------------------------------------------------
class TestNormalizationStages(unittest.TestCase):
    def test_stage_a_strips_bidi_keeps_harakat(self):
        self.assertEqual(cleanup_ocr_text("\u200fطَرَقَ"), "طَرَقَ")
        self.assertEqual(cleanup_ocr_text("\u202eذهب\u202c"), "ذهب")

    def test_stage_a_folds_arabic_presentation_forms(self):
        self.assertEqual(cleanup_ocr_text("\uFEFB"), "لا")   # ARABIC LIGATURE LAM WITH ALEF
        self.assertEqual(cleanup_ocr_text("ﺫﻫﺐ"), "ذهب")     # isolated presentation forms

    def test_stage_b_strips_edge_punctuation(self):
        self.assertEqual(dictionary_normalize("ذهب،"), "ذهب")
        self.assertEqual(dictionary_normalize("(رحمه)"), "رحمه")

    def test_stage_b_reuses_dictionary_normalizer(self):
        for spelling in ("ذهب", "ذَهَبَ", "طَرَقَ", "كتب", "عمل"):
            self.assertEqual(dictionary_normalize(spelling), normalize_arabic(spelling))

    def test_arabic_detection(self):
        self.assertTrue(is_arabic_word("ذهب"))
        self.assertTrue(is_arabic_word("طَرَقَ"))
        self.assertFalse(is_arabic_word("hello"))
        self.assertFalse(is_arabic_word(""))


# --------------------------------------------------------------------------
# 4. dictionary integration
# --------------------------------------------------------------------------
class TestDictionaryIntegration(unittest.TestCase):
    def test_goes_lookup_exact_then_normalized_all_candidates(self):
        # bare form: no exact DB row -> normalized fallback fires
        r = dblookup.lookup("ذهب", "ذهب")
        self.assertTrue(r.found)
        self.assertEqual(r.match_type, "normalized")
        self.assertTrue(r.is_ambiguous)                       # never a single winner
        self.assertGreaterEqual(r.candidate_count, 2)
        self.assertGreaterEqual(len(r.distinct_spellings), 2) # ذَهَب + ذَهَبَ share key
        malayalam = " ".join(e.malayalam or "" for e in r.entries)
        self.assertIn("പോവുക", malayalam)

    def test_ambiguous_kitaba_returns_every_candidate(self):
        r = dblookup.lookup("كتب", "كتب")
        self.assertTrue(r.found)
        self.assertGreaterEqual(r.candidate_count, 3)

    def test_pipeline_word_reaches_the_dictionary(self):
        with open(DATASET_DIR / "pages" / "001.json", encoding="utf-8") as fh:
            page = json.load(fh)
        by_norm = {w["text_normalized"]: w for w in page["words"]}
        for key in ("ذهب", "كتب", "بيت"):
            w = by_norm[key]
            r = dblookup.lookup(w["text_clean"], w["text_normalized"])
            self.assertTrue(r.found, f"OCR word {w['text']!r} must resolve")
            self.assertGreaterEqual(r.candidate_count, 1)

    def test_lookup_module_never_limits(self):
        # both lookup queries must return every candidate row -- no LIMIT.
        self.assertNotIn("LIMIT", dblookup._SQL_EXACT.upper())
        self.assertNotIn("LIMIT", dblookup._SQL_NORMALIZED.upper())


# --------------------------------------------------------------------------
# 5. resume / identity / failure-continue
# --------------------------------------------------------------------------
class _FailingPageBackend(OCRBackend):
    """Tesseract backend that raises for one specific page."""

    name = "tesseract-fail-page"

    def __init__(self, fail_on: int = 2):
        self._inner = TesseractBackend()
        self._fail_on = fail_on

    @property
    def version(self) -> str:
        return self._inner.version

    @property
    def language(self) -> str:
        return self._inner.language

    def recognize_page(self, image, page_number: int = 1):
        if page_number == self._fail_on:
            raise RuntimeError("synthetic failure for page")
        return self._inner.recognize_page(image, page_number=page_number)


class TestResumeIdentityAndFailures(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ocr-test-")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _out(self, name="out") -> Path:
        return Path(self._tmp) / name

    def test_resume_skips_valid_pages(self):
        out = self._out()
        first = run_pipeline(SCANNED_PDF, out, limit=1)
        statuses = [s["status"] for s in first["page_stats"]]
        self.assertEqual(statuses, ["ok", "pending"])
        page1_path = out / "pages" / "001.json"
        before = page1_path.read_bytes()

        second = run_pipeline(SCANNED_PDF, out)   # resumes without --force
        statuses2 = [s["status"] for s in second["page_stats"]]
        self.assertEqual(statuses2, ["skipped", "ok"])
        self.assertEqual(page1_path.read_bytes(), before,
                         "resume must not rewrite a valid page file")
        self.assertEqual(second["totals"]["total_words"], 32)

    def test_identity_guard_sha_mismatch(self):
        out = self._out()
        run_pipeline(SCANNED_PDF, out, limit=1)
        with self.assertRaises(PdfIdentityError):
            run_pipeline(TEXT_PDF, out)   # different PDF, same output dir

    def test_identity_guard_pages_without_manifest(self):
        out = self._out()
        pages = out / "pages"
        pages.mkdir(parents=True)
        (pages / "001.json").write_text(
            json.dumps({"page": 1, "image_width": 100, "image_height": 100,
                        "words": []}), encoding="utf-8")
        with self.assertRaises(PdfIdentityError):
            run_pipeline(SCANNED_PDF, out)

    def test_failed_page_reported_and_others_continue(self):
        out = self._out()
        summary = run_pipeline(SCANNED_PDF, out, backend=_FailingPageBackend(2))
        self.assertEqual(summary["totals"]["failed_pages"], [2])
        self.assertEqual(summary["manifest"]["status"], "complete_with_errors")
        self.assertEqual([e["page"] for e in summary["errors"]], [2])
        self.assertTrue((out / "pages" / "001.json").is_file())   # page 1 done
        self.assertFalse((out / "pages" / "002.json").exists())   # page 2 not written
        with open(out / "ocr_report.json", encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual(report["failed_pages"], [2])

    def test_force_rebuilds(self):
        out = self._out()
        run_pipeline(SCANNED_PDF, out, limit=1)
        run_pipeline(SCANNED_PDF, out, force=True)
        self.assertEqual(len(list((out / "pages").glob("*.json"))), 2)


# --------------------------------------------------------------------------
# 6. CLI
# --------------------------------------------------------------------------
class TestCLI(unittest.TestCase):
    def test_cli_run_with_limit(self):
        with tempfile.TemporaryDirectory(prefix="ocr-cli-") as tmp:
            out = Path(tmp) / "out"
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "ocr/ocr_pdf.py",
                 str(SCANNED_PDF), "-o", str(out), "--limit", "1"],
                capture_output=True, text=True, encoding="utf-8",
                cwd=str(ROOT), timeout=180,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("OCR engine", proc.stdout)
            manifest = load_json(out / "manifest.json")
            self.assertIsNotNone(manifest)
            self.assertEqual(manifest["status"], "complete")
            self.assertTrue((out / "pages" / "001.json").is_file())
            self.assertFalse((out / "pages" / "002.json").exists())
            csv_path = out / "ocr_report.csv"
            self.assertTrue(csv_path.is_file())
            self.assertTrue(csv_path.read_text(encoding="utf-8")
                            .startswith("page,word_count"))


# --------------------------------------------------------------------------
# 7. reader end-to-end (real headless Chrome)
# --------------------------------------------------------------------------
def _make_handler(directory: Path):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, *args):   # keep test output quiet
            pass
    return Handler


def _chrome_available() -> bool:
    try:
        headless_chrome.find_chrome_path()
        return True
    except ChromeError:
        return False


@unittest.skipUnless(_chrome_available(), "Chrome not found")
class TestReaderEndToEnd(unittest.TestCase):
    """Drive reader.html in a real headless Chrome over CDP."""

    @classmethod
    def setUpClass(cls):
        cls._server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _make_handler(ROOT))
        cls._port = cls._server.server_address[1]
        threading.Thread(target=cls._server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls._port}/reader.html"
        cls.chrome = Chrome.open("about:blank")
        cls.chrome.navigate(cls.url)
        cls.chrome.wait_for("typeof dictRows !== 'undefined' && dictRows.length > 0",
                            timeout=60)
        cls.chrome.wait_for("exactMap && exactMap.size > 0", timeout=60)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.chrome.close()
        finally:
            cls._server.shutdown()
            cls._server.server_close()

    def _open_pdf(self, path: Path, wait_expr: str):
        self.chrome.set_file_input_files("#fileInput", [str(path)])
        self.chrome.dispatch_event("#fileInput")
        self.chrome.wait_for(wait_expr, timeout=60)

    def test_scanned_pdf_lazy_ocr_overlay(self):
        chrome = self.chrome
        self._open_pdf(SCANNED_PDF,
                       "ocrState.manifest && "
                       "typeof ocrState.manifest.page_count === 'number' && "
                       "document.querySelectorAll('.ocr-word').length > 0")
        # lazy: only the visible page carries an overlay
        page = chrome.evaluate("ocrState.manifest")
        self.assertEqual(page["page_count"], 2)
        self.assertIsInstance(page["pdf_sha256"], str)
        self.assertEqual(len(page["pdf_sha256"]), 64)
        word_count = chrome.evaluate("document.querySelectorAll('.ocr-word').length")
        self.assertGreaterEqual(word_count, 14, "page 1 must carry its ~14 words")
        # park the mouse away from the page so no box is :hover-highlighted
        chrome.send("Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": 5, "y": 5})
        # fully transparent until hovered/selected, so the scan stays readable
        bg = chrome.wait_for(
            "getComputedStyle(document.querySelector('.ocr-word')).backgroundColor"
            " === 'rgba(0, 0, 0, 0)' ? 'transparent' : ''")
        self.assertEqual(bg, "transparent")
        # the status bar confirms the overlay is active (once loading finishes)
        status = chrome.wait_for(
            "document.getElementById('status').textContent.includes('Ready')"
            " ? document.getElementById('status').textContent : ''")
        self.assertIn("OCR overlay", status)
        # no text-layer spans yet -- this is a scanned (image-only) PDF
        spans = chrome.evaluate(
            "document.querySelectorAll('.pdf-page .textLayer span').length")
        self.assertEqual(spans, 0)

    def test_click_word_shows_dictionary_popup(self):
        chrome = self.chrome
        if chrome.evaluate("document.querySelectorAll('.ocr-word').length === 0"):
            self._open_pdf(SCANNED_PDF,
                           "ocrState.manifest && "
                           "document.querySelectorAll('.ocr-word').length > 0")
        x, y = chrome.element_center('.ocr-word[data-norm="ذهب"]')
        chrome.click(x, y)
        popup_text = chrome.wait_for(
            "(() => { const p = document.getElementById('lookupPopup');"
            " return p.classList.contains('hidden') ? '' : p.innerText; })()")
        self.assertIn("പോവുക", popup_text)              # Malayalam meaning shown
        self.assertIn("ذهب", popup_text)
        # sidebar lookup panel populated as well
        self.assertFalse(chrome.evaluate(
            "document.getElementById('lookupPanel').classList.contains('hidden')"))
        # clicked word carries the selection highlight
        self.assertEqual(chrome.evaluate(
            "document.querySelectorAll('.ocr-word.selected').length"), 1)

    def test_text_pdf_path_regression(self):
        chrome = self.chrome
        self._open_pdf(TEXT_PDF,
                       "document.querySelectorAll('.pdf-page .textLayer span').length > 0")
        self.assertEqual(chrome.evaluate("document.querySelectorAll('.ocrLayer').length"),
                         0, "text PDF must not get an OCR overlay")
        self.assertIsNone(chrome.evaluate("ocrState.manifest"),
                          "manifest must be reset when opening a non-OCR PDF")
        status = chrome.wait_for(
            "document.getElementById('status').textContent.includes('Ready')"
            " ? document.getElementById('status').textContent : ''")
        self.assertNotIn("OCR overlay", status)


if __name__ == "__main__":
    unittest.main(verbosity=2)