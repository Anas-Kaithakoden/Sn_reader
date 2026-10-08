#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline OCR preprocessing pipeline for scanned / image-based Arabic PDFs.

    python ocr/ocr_pdf.py input.pdf --output data/ocr/<name>
    python ocr/ocr_pdf.py book.pdf --output ocr_book/ --dpi 300
    python ocr/ocr_pdf.py book.pdf --force          # reprocess everything

What it does (once, offline -- no cloud OCR, no internet):

    scanned PDF -> render each page (~300 DPI) -> Arabic OCR ->
    word-level bounding boxes -> conservative Unicode cleanup ->
    dictionary normalization -> pages/NNN.json + manifest + reports

The result is a self-contained dataset that ``reader.html`` lazy-loads per
visible page, so the reader never runs OCR itself.

Design notes
------------
* one page at a time: rendering, OCR and JSON writing happen page by page,
  so a 500-page book never sits in RAM and every finished page is on disk
  immediately;
* resumable: existing *valid* ``pages/NNN.json`` files are skipped (unless
  ``--force``), so an interrupted run continues where it stopped;
* identity-safe: the SHA-256 of the input PDF is stored in ``manifest.json``
  and re-checked on every run -- OCR data from two different PDFs is never
  silently mixed;
* swappable engine: the pipeline only speaks to ``ocr.ocr_backend.OCRBackend``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make project-root imports work both as `python ocr/ocr_pdf.py` (from the
# repo root or from inside ocr/) and as `import ocr.ocr_pdf`.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pymupdf  # PyMuPDF
from PIL import Image

from ocr.normalize_ocr import cleanup_ocr_text, dictionary_normalize, is_arabic_word
from ocr.ocr_schema import (
    PAGE_DIR,
    SCHEMA_VERSION,
    assign_reading_order,
    detect_overlapping_words,
    load_json,
    make_manifest,
    make_page_doc,
    make_word_doc,
    page_filename,
    validate_page_doc,
)
from ocr.tesseract_backend import TesseractBackend

__all__ = [
    "PipelineError",
    "PdfIdentityError",
    "sha256_file",
    "render_page_image",
    "scan_pdf_content",
    "make_backend",
    "run_pipeline",
    "main",
]

DEFAULT_DPI = 300
DEFAULT_LANGUAGE = "ara"
DEFAULT_CONFIDENCE_THRESHOLD = 60.0
DEFAULT_OVERLAP_THRESHOLD = 0.5
DEFAULT_OUTPUT_ROOT = _ROOT / "data" / "ocr"


class PipelineError(RuntimeError):
    """Fatal, user-facing pipeline error."""


class PdfIdentityError(PipelineError):
    """The output directory does not belong to this input PDF."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    """Streaming SHA-256 -- never loads the whole PDF into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_json(path: Path, obj, *, indent: int | None = 2) -> None:
    """Write JSON via a temp file so readers never see a half-written page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(obj, ensure_ascii=False, indent=indent,
                      separators=(",", ":") if indent is None else None)
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(data)
    os.replace(tmp, path)


def render_page_image(doc: "pymupdf.Document", page_number: int,
                      dpi: int = DEFAULT_DPI, save_to: Path | None = None) -> Image.Image:
    """Rasterize one PDF page at ``dpi`` (only this page is in memory).

    The returned image is the coordinate system every OCR bounding box and
    every stored ``image_width``/``image_height`` refers to.
    """
    page = doc[page_number - 1]
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    comps = pix.n - pix.alpha
    mode = {1: "L", 3: "RGB", 4: "CMYK"}.get(comps)
    if mode is None:
        raise PipelineError(
            f"unsupported pixmap layout (n={pix.n}, alpha={pix.alpha}) "
            f"on page {page_number}"
        )
    img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    if mode != "RGB":
        img = img.convert("RGB")
    if save_to is not None:
        save_to.parent.mkdir(parents=True, exist_ok=True)
        img.save(save_to, format="PNG")
    return img


def scan_pdf_content(doc: "pymupdf.Document") -> dict:
    """Classify the PDF: does it carry a text layer, images, or both?

    Uses ``get_text``/``get_images`` only -- no rasterization, so this is
    cheap even for large books.
    """
    pages_with_text = 0
    pages_with_images = 0
    for page in doc:
        if len(page.get_text().strip()) >= 10:
            pages_with_text += 1
        if page.get_images():
            pages_with_images += 1
    total = doc.page_count
    if pages_with_text == 0:
        kind = "scanned"
    elif pages_with_text >= total:
        kind = "text"
    else:
        kind = "mixed"
    return {
        "kind": kind,
        "page_count": total,
        "pages_with_text": pages_with_text,
        "pages_with_images": pages_with_images,
    }


def make_backend(engine: str = "tesseract", *, language: str = DEFAULT_LANGUAGE,
                 psm: str | int = "auto"):
    """Engine factory -- the only place that knows concrete engine classes."""
    if engine == "tesseract":
        return TesseractBackend(language=language, psm=psm)
    raise PipelineError(
        f"unknown OCR engine {engine!r} (available: tesseract)"
    )


def default_output_dir(input_pdf: Path) -> Path:
    return DEFAULT_OUTPUT_ROOT / input_pdf.stem


def output_size_bytes(output_dir: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(output_dir):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


# --------------------------------------------------------------------------
# per-page statistics
# --------------------------------------------------------------------------
def stats_from_page_doc(page_doc: dict, status: str = "ok") -> dict:
    words = page_doc.get("words", [])
    n = len(words)
    arabic = sum(1 for w in words if w.get("is_arabic"))
    low = sum(1 for w in words if w.get("low_confidence"))
    overlap = sum(1 for w in words if w.get("overlap"))
    conf_sum = sum(float(w.get("confidence", 0.0)) for w in words)
    return {
        "page": page_doc.get("page"),
        "status": status,
        "word_count": n,
        "arabic_word_count": arabic,
        "non_arabic_token_count": n - arabic,
        "confidence_sum": round(conf_sum, 1),
        "average_confidence": round(conf_sum / n, 2) if n else 0.0,
        "low_confidence_count": low,
        "overlap_count": overlap,
        "processing_time": float(page_doc.get("processing_time", 0.0) or 0.0),
    }


def failed_page_stats(page: int, elapsed: float) -> dict:
    return {
        "page": page,
        "status": "failed",
        "word_count": 0,
        "arabic_word_count": 0,
        "non_arabic_token_count": 0,
        "confidence_sum": 0.0,
        "average_confidence": 0.0,
        "low_confidence_count": 0,
        "overlap_count": 0,
        "processing_time": round(elapsed, 3),
    }


def pending_page_stats(page: int) -> dict:
    stats = failed_page_stats(page, 0.0)
    stats["status"] = "pending"
    return stats


def aggregate_stats(page_stats: list[dict]) -> dict:
    done = [s for s in page_stats if s["status"] in ("ok", "skipped")]
    total_words = sum(s["word_count"] for s in done)
    conf_sum = sum(s["confidence_sum"] for s in done)
    return {
        "total_words": total_words,
        "arabic_words": sum(s["arabic_word_count"] for s in done),
        "non_arabic_tokens": sum(s["non_arabic_token_count"] for s in done),
        "low_confidence_count": sum(s["low_confidence_count"] for s in done),
        "average_confidence": round(conf_sum / total_words, 2) if total_words else 0.0,
        "overlap_count": sum(s["overlap_count"] for s in done),
        "pages_with_zero_detected_words": sorted(
            s["page"] for s in done if s["word_count"] == 0
        ),
        "pages_processed": sum(1 for s in page_stats if s["status"] == "ok"),
        "pages_skipped": sum(1 for s in page_stats if s["status"] == "skipped"),
        "failed_pages": sorted(s["page"] for s in page_stats if s["status"] == "failed"),
        "pending_pages": sorted(s["page"] for s in page_stats if s["status"] == "pending"),
    }


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------
def build_report(*, input_pdf: Path, manifest: dict, totals: dict,
                 processing_time_s: float, errors: list[dict],
                 output_dir: Path) -> dict:
    return {
        "pdf": input_pdf.name,
        "page_count": manifest["page_count"],
        "ocr_engine": manifest["ocr_engine"],
        "ocr_engine_version": manifest["ocr_engine_version"],
        "language": manifest["language"],
        "dpi": manifest["dpi"],
        "processing_time_s": round(processing_time_s, 2),
        "total_words": totals["total_words"],
        "arabic_words": totals["arabic_words"],
        "non_arabic_tokens": totals["non_arabic_tokens"],
        "low_confidence_count": totals["low_confidence_count"],
        "average_confidence": totals["average_confidence"],
        "pages_with_zero_detected_words": totals["pages_with_zero_detected_words"],
        "overlap_count": totals["overlap_count"],
        "failed_pages": totals["failed_pages"],
        "pages_pending": totals["pending_pages"],
        "errors": errors,
        "output_dir": str(output_dir),
        "generated_at": utc_now(),
    }


REPORT_CSV_COLUMNS = [
    "page",
    "word_count",
    "arabic_word_count",
    "average_confidence",
    "low_confidence_count",
    "overlap_count",
    "processing_time",
]


def write_report_csv(path: Path, page_stats: list[dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_CSV_COLUMNS)
        writer.writeheader()
        for s in sorted(page_stats, key=lambda s: s["page"]):
            writer.writerow({
                "page": s["page"],
                "word_count": s["word_count"],
                "arabic_word_count": s["arabic_word_count"],
                "average_confidence": s["average_confidence"],
                "low_confidence_count": s["low_confidence_count"],
                "overlap_count": s["overlap_count"],
                "processing_time": round(s["processing_time"], 3),
            })
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# identity / resume
# --------------------------------------------------------------------------
def _load_existing_dataset(output_dir: Path, sha: str, dpi: int, language: str,
                           force: bool) -> dict | None:
    """Validate an existing dataset against this PDF + settings.

    Returns the existing manifest when the dataset may be reused, ``None``
    for a fresh directory.  Raises :class:`PdfIdentityError` when page files
    exist that cannot be tied to this PDF, or when the settings differ
    (unless ``--force`` rebuilds the directory).
    """
    manifest_path = output_dir / "manifest.json"
    pages_dir = output_dir / PAGE_DIR
    existing = load_json(manifest_path)
    has_pages = pages_dir.is_dir() and any(pages_dir.glob("*.json"))

    if existing is None:
        if has_pages:
            if force:
                shutil.rmtree(pages_dir, ignore_errors=True)
                return None
            raise PdfIdentityError(
                f"{output_dir} contains OCR page files but no readable "
                "manifest.json, so they cannot be tied to this PDF. "
                "Re-run with --force to rebuild the directory."
            )
        return None

    stored_sha = existing.get("pdf_sha256")
    if stored_sha and stored_sha != sha:
        if force:
            shutil.rmtree(pages_dir, ignore_errors=True)
            manifest_path.unlink(missing_ok=True)
            return None
        raise PdfIdentityError(
            f"{output_dir} belongs to a different PDF "
            f"(manifest sha256 {stored_sha[:12]}…, this PDF {sha[:12]}…). "
            "Use a different --output directory, or --force to rebuild."
        )

    # Same PDF, but pages rendered at another scale / recognized in another
    # language must not be mixed with fresh ones.
    if int(existing.get("dpi", dpi)) != int(dpi) or \
            str(existing.get("language", language)) != str(language):
        if force:
            shutil.rmtree(pages_dir, ignore_errors=True)
            manifest_path.unlink(missing_ok=True)
            return None
        raise PdfIdentityError(
            f"{output_dir} was created with dpi={existing.get('dpi')}, "
            f"language={existing.get('language')!r}; this run uses "
            f"dpi={dpi}, language={language!r}. Re-run with --force to rebuild."
        )

    if force:
        shutil.rmtree(pages_dir, ignore_errors=True)
        manifest_path.unlink(missing_ok=True)
        return None

    return existing


def load_valid_page(path: Path, page: int) -> dict | None:
    """Return the page document when the file exists, parses and validates."""
    if not path.is_file():
        return None
    obj = load_json(path)
    if obj is None:
        return None
    return obj if validate_page_doc(obj, page) else None


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------
def run_pipeline(input_pdf: str | Path, output_dir: str | Path | None = None,
                 *, dpi: int = DEFAULT_DPI,
                 language: str = DEFAULT_LANGUAGE,
                 psm: str | int = "auto",
                 confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
                 overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD,
                 force: bool = False,
                 keep_images: bool = False,
                 limit: int | None = None,
                 engine: str = "tesseract",
                 backend=None) -> dict:
    """Run (or resume) the OCR preprocessing for one PDF.

    Returns a summary dict with totals, report, failed pages etc.  Raises
    :class:`PdfIdentityError` when ``output_dir`` holds another PDF's data.
    """
    input_pdf = Path(input_pdf).expanduser().resolve()
    if not input_pdf.is_file():
        raise PipelineError(f"input PDF not found: {input_pdf}")
    if output_dir is None:
        output_dir = default_output_dir(input_pdf)
    output_dir = Path(output_dir).expanduser().resolve()

    started = time.perf_counter()
    started_at = utc_now()

    pdf_sha = sha256_file(input_pdf)
    existing_manifest = _load_existing_dataset(
        output_dir, pdf_sha, dpi, language, force
    )

    if backend is None:
        backend = make_backend(engine, language=language, psm=psm)

    pages_dir = output_dir / PAGE_DIR
    pages_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"

    doc = pymupdf.open(input_pdf)
    try:
        page_count = doc.page_count
        content = scan_pdf_content(doc)

        # ---- manifest: keep original created_at when resuming ------------
        if existing_manifest is not None:
            manifest = dict(existing_manifest)
            manifest["page_count"] = page_count
            manifest["schema_version"] = SCHEMA_VERSION
        else:
            manifest = make_manifest(
                source_pdf=input_pdf.name,
                pdf_sha256=pdf_sha,
                page_count=page_count,
                engine=backend.name,
                engine_version=backend.version,
                language=language,
                dpi=dpi,
                created_at=started_at,
            )
        manifest.update(
            status="in_progress",
            psm=str(psm),
            confidence_threshold=float(confidence_threshold),
            pdf_size_bytes=input_pdf.stat().st_size,
            content=content,
        )
        atomic_write_json(output_dir / "manifest.json", manifest)

        # ---- page loop: one page at a time, write immediately ------------
        page_stats: list[dict] = []
        errors: list[dict] = []
        limit_pages = page_count if limit is None else min(page_count, int(limit))

        for page_no in range(1, page_count + 1):
            page_path = pages_dir / page_filename(page_no, page_count)

            if page_no > limit_pages:
                page_stats.append(pending_page_stats(page_no))
                continue

            if not force:
                existing_page = load_valid_page(page_path, page_no)
                if existing_page is not None:
                    page_stats.append(stats_from_page_doc(existing_page, "skipped"))
                    continue

            t0 = time.perf_counter()
            try:
                save_to = images_dir / page_filename(page_no, page_count).replace(
                    ".json", ".png") if keep_images else None
                image = render_page_image(doc, page_no, dpi=dpi, save_to=save_to)

                result = backend.recognize_page(image, page_number=page_no)
                del image

                words = []
                for w in result.words:
                    words.append(
                        make_word_doc(
                            text=w.text,                       # raw OCR, untouched
                            text_clean=cleanup_ocr_text(w.text),
                            text_normalized=dictionary_normalize(w.text),
                            confidence=w.confidence,
                            bbox=w.bbox,
                            is_arabic=is_arabic_word(w.text),
                            low_confidence=w.confidence < confidence_threshold,
                            reading_order=0,
                            block=w.block,
                            paragraph=w.paragraph,
                            line=w.line,
                            word_index=w.word_index,
                        )
                    )
                assign_reading_order(words)
                detect_overlapping_words(words, threshold=overlap_threshold)

                page_doc = make_page_doc(
                    page_no,
                    result.image_width,
                    result.image_height,
                    words,
                    processing_time=time.perf_counter() - t0,
                    processed_at=utc_now(),
                    meta=result.meta or None,
                )
                atomic_write_json(page_path, page_doc, indent=None)
                page_stats.append(stats_from_page_doc(page_doc, "ok"))

            except Exception as exc:                     # keep going, always
                elapsed = time.perf_counter() - t0
                message = f"{type(exc).__name__}: {exc}"
                errors.append({"page": page_no, "error": message})
                page_stats.append(failed_page_stats(page_no, elapsed))
                print(f"  page {page_no}: FAILED -- {message}", file=sys.stderr)
                continue

        # ---- aggregate + write reports ----------------------------------
        totals = aggregate_stats(page_stats)
        processing_time_s = time.perf_counter() - started
        completed_at = utc_now()

        manifest.update(
            status="complete" if not totals["failed_pages"] else "complete_with_errors",
            completed_at=completed_at,
            total_words=totals["total_words"],
            failed_pages=totals["failed_pages"],
        )
        atomic_write_json(output_dir / "manifest.json", manifest)

        report = build_report(
            input_pdf=input_pdf,
            manifest=manifest,
            totals=totals,
            processing_time_s=processing_time_s,
            errors=errors,
            output_dir=output_dir,
        )
        atomic_write_json(output_dir / "ocr_report.json", report)
        write_report_csv(output_dir / "ocr_report.csv", page_stats)

        metadata = {
            "schema_version": SCHEMA_VERSION,
            "pipeline": {
                "command": sys.argv,
                "started_at": started_at,
                "finished_at": completed_at,
                "processing_time_s": round(processing_time_s, 2),
                "dpi": dpi,
                "language": language,
                "psm": str(psm),
                "engine": backend.name,
                "engine_version": backend.version,
                "confidence_threshold": float(confidence_threshold),
                "overlap_threshold": float(overlap_threshold),
                "force": bool(force),
                "keep_images": bool(keep_images),
                "limit": limit,
            },
            "pdf": {
                "path": str(input_pdf),
                "filename": input_pdf.name,
                "sha256": pdf_sha,
                "size_bytes": input_pdf.stat().st_size,
                "page_count": page_count,
                "content": content,
            },
            "totals": totals,
            "pages": page_stats,
            "errors": errors,
        }
        atomic_write_json(output_dir / "metadata.json", metadata)

        return {
            "input_pdf": str(input_pdf),
            "output_dir": str(output_dir),
            "manifest": manifest,
            "report": report,
            "page_stats": page_stats,
            "errors": errors,
            "totals": totals,
            "content": content,
            "output_size_bytes": output_size_bytes(output_dir),
            "processing_time_s": round(processing_time_s, 2),
        }

    finally:
        doc.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ocr_pdf.py",
        description="Offline OCR preprocessing for scanned Arabic PDFs "
                    "(word-level bounding boxes for reader.html).",
    )
    p.add_argument("input", help="input PDF file (may be image-only / scanned)")
    p.add_argument("-o", "--output", default=None,
                   help="output directory (default: data/ocr/<pdf-name>/)")
    p.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                   help=f"render resolution (default: {DEFAULT_DPI})")
    p.add_argument("--language", default=DEFAULT_LANGUAGE,
                   help=f"Tesseract language(s) (default: {DEFAULT_LANGUAGE})")
    p.add_argument("--psm", default="auto",
                   help="Tesseract page segmentation mode, or 'auto' "
                        "(try 6 then 3; default: auto)")
    p.add_argument("--confidence-threshold", type=float,
                   default=DEFAULT_CONFIDENCE_THRESHOLD,
                   help="mark words below this confidence as low_confidence "
                        f"(default: {DEFAULT_CONFIDENCE_THRESHOLD:g}; words are "
                        "kept either way)")
    p.add_argument("--overlap-threshold", type=float,
                   default=DEFAULT_OVERLAP_THRESHOLD,
                   help="overlap ratio treated as suspicious (default: 0.5)")
    p.add_argument("--engine", default="tesseract",
                   help="OCR engine id (default: tesseract)")
    p.add_argument("--force", action="store_true",
                   help="reprocess every page, overwriting existing results")
    p.add_argument("--keep-images", action="store_true",
                   help="also save the rendered page images (large!)")
    p.add_argument("--limit", type=int, default=None,
                   help="only process the first N pages (resume later)")
    return p


def print_summary(summary: dict) -> None:
    m = summary["manifest"]
    t = summary["totals"]
    r = summary["report"]
    failed = t["failed_pages"]
    pending = t["pending_pages"]
    print(f"OCR engine      : {m['ocr_engine']} {m['ocr_engine_version']} "
          f"(language={m['language']}, psm={m.get('psm')})")
    print(f"PDF             : {Path(summary['input_pdf']).name} "
          f"({m['page_count']} pages, content={summary['content']['kind']})")
    print(f"Pages           : {t['pages_processed']} processed, "
          f"{t['pages_skipped']} skipped (already done), "
          f"{len(failed)} failed"
          + (f", {len(pending)} pending" if pending else ""))
    print(f"Words           : {t['total_words']} total, "
          f"{t['arabic_words']} Arabic, {t['non_arabic_tokens']} non-Arabic")
    print(f"Confidence      : avg {t['average_confidence']}, "
          f"{t['low_confidence_count']} below threshold "
          f"({r['dpi']} DPI)")
    print(f"Overlaps flagged: {t['overlap_count']}   "
          f"zero-word pages: {t['pages_with_zero_detected_words'] or 'none'}")
    print(f"OCR output size : {summary['output_size_bytes']} bytes")
    print(f"Processing time : {summary['processing_time_s']} s")
    print(f"Output          : {summary['output_dir']}")
    if failed:
        print(f"FAILED PAGES    : {failed}", file=sys.stderr)
        for e in summary["errors"]:
            print(f"  page {e['page']}: {e['error']}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    args = build_arg_parser().parse_args(argv)
    try:
        summary = run_pipeline(
            args.input,
            args.output,
            dpi=args.dpi,
            language=args.language,
            psm=args.psm,
            confidence_threshold=args.confidence_threshold,
            overlap_threshold=args.overlap_threshold,
            force=args.force,
            keep_images=args.keep_images,
            limit=args.limit,
            engine=args.engine,
        )
    except (PipelineError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print_summary(summary)
    return 1 if summary["totals"]["failed_pages"] else 0


if __name__ == "__main__":
    sys.exit(main())
