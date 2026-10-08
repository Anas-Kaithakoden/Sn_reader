# OCR pipeline (scanned / image-only Arabic PDFs)

Turns scanned Arabic PDFs into **word-level OCR JSON** once, offline, so the
reader can show a tappable word overlay **without ever running OCR in the
browser**.

```
scanned PDF ──► render page (~300 DPI) ──► Tesseract (ara, offline)
        ──► word boxes ──► conservative cleanup ──► dictionary normalize
        ──► pages/NNN.json + manifest.json + reports
```

## Quick start

```bash
# one-off run for a scanned book
python ocr/ocr_pdf.py my_book.pdf                     # -> data/ocr/my_book/

# the reader auto-detects the dataset named after the PDF (same stem) and
# lazy-loads one page JSON at a time:
#   data/ocr/<pdf-name>/manifest.json
#   data/ocr/<pdf-name>/pages/001.json, 002.json, ...
```

| flag              | meaning                                                          |
|-------------------|------------------------------------------------------------------|
| `-o, --output`    | output dir (default `data/ocr/<pdf-name>/`)                      |
| `--dpi`           | render resolution (default **300**)                              |
| `--language`      | Tesseract languages (default `ara`)                              |
| `--psm`           | `auto` = try 6 then 3, or pin a mode (default `auto`)            |
| `--confidence-threshold` | words below this are marked `low_confidence` (default 60; never deleted) |
| `--overlap-threshold`    | overlap ratio used to flag duplicated boxes (default 0.5; never removed) |
| `--force`         | reprocess every page (identity check still applies)              |
| `--keep-images`   | also save the rendered page PNGs (large!)                        |
| `--limit N`       | process only the first N pages (resume later)                    |
| `--engine`        | OCR engine id (default `tesseract`)                              |

Run `python ocr/ocr_pdf.py --help` for the full list.

## What the browser does (and does not) do

* `reader.html` fetches only the JSON of **visible pages** (via
  `manifest.page_number_width`/`page_file_pattern`), scales the word boxes
  onto the displayed page with a single aspect-ratio scale factor, and lays
  transparent click/tap regions over the page image.
* Tapping a word runs the **same dictionary lookup as the text layer**
  (exact `text_clean` → stored `text_normalized` fallback, **all** candidates
  shown — the app never picks a winner).
* SHA-256 of the opened PDF is verified against `manifest.pdf_sha256`, so
  overlay data from a different/renamed PDF is never attached.
* No OCR engine, no cloud service, no CDN — everything is offline and local.

## Word entry schema (schema_version 1)

| key               | meaning                                                          |
|-------------------|------------------------------------------------------------------|
| `text`            | **raw OCR output, untouched** (harakat preserved)                |
| `text_clean`      | stage A: strip bidi/format chars + NFKC + NFC (conservative)     |
| `text_normalized` | stage B: dictionary lookup key (`normalized_buhaira` algorithm)  |
| `confidence`      | engine confidence (0–100)                                        |
| `is_arabic`       | token is Arabic-looking                                          |
| `low_confidence`  | below the confidence threshold — **kept, never deleted**         |
| `overlap`         | suspicious box overlap — **flagged, never deleted**              |
| `reading_order`   | deterministic 0-based index: block→paragraph→line→**RTL** inside line |
| `bbox`            | `{x, y, width, height}` in **rendered-page pixels** at `dpi`     |
| `block/paragraph/line/word_index` | engine structure (for later analysis)         |

Pages are written atomically (tmp + rename), compact, as soon as each page
finishes -- the pipeline can be interrupted and resumed (valid page files are
skipped), and a failed page never blocks the rest of the book.

## Files

| file                       | purpose                                             |
|----------------------------|-----------------------------------------------------|
| `ocr_pdf.py`               | CLI + pipeline (`run_pipeline`, resumable)          |
| `ocr_backend.py`           | `OCRBackend` ABC (swap engines without touching anything else) |
| `tesseract_backend.py`     | Tesseract implementation, Arabic by default         |
| `normalize_ocr.py`         | stage A cleanup vs stage B dictionary normalization |
| `ocr_schema.py`            | page/manifest schema, reading order, overlap scan   |
| `make_test_pdf.py`         | build the image-only test PDF (`data/test_pdfs/`)   |
| `../headless_chrome.py`    | stdlib CDP client for the browser end-to-end tests  |
| `../test_ocr_pipeline.py`  | full test suite (unit + CLI + real headless Chrome) |

## Tests

```bash
.venv\Scripts\python.exe -X utf8 test_ocr_pipeline.py
```

Requires Tesseract with `ara` on `PATH` (or `TESSERACT_CMD`), and Chrome for
the reader end-to-end tests (`CHROME_PATH` override supported). Fixtures are
recreated automatically when missing.