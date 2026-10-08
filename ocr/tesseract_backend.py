# -*- coding: utf-8 -*-
"""Tesseract implementation of :class:`ocr.ocr_backend.OCRBackend`.

Requires an offline Tesseract installation with Arabic language data
(``tesseract --list-langs`` must contain ``ara``).  No network access is used.

Word segmentation comes from Tesseract itself (``image_to_data``), so the
engine's own block / paragraph / line / word structure and bounding boxes are
preserved -- words are never re-derived by splitting text on whitespace.

Page-segmentation modes
-----------------------
``psm="auto"`` (the default) tries ``--psm 6`` (a uniform block of text --
the common case for a book page) and falls back to ``--psm 3`` (Tesseract's
automatic segmentation) when the first attempt finds nothing.  A concrete
``--psm`` value pins the mode.
"""

from __future__ import annotations

import shutil

import pytesseract
from PIL import Image

from ocr.ocr_backend import OCRBackend, OCRPageResult, OCRWord

__all__ = ["TesseractBackend", "TESSERACT_AUTO_PSM_MODES"]

#: tried in order when psm="auto"
TESSERACT_AUTO_PSM_MODES = (6, 3)


def _find_tesseract() -> str | None:
    """Locate the tesseract executable, honouring a manual override."""
    import os

    override = os.environ.get("TESSERACT_CMD")
    if override:
        return override
    return shutil.which("tesseract")


class TesseractBackend(OCRBackend):
    """Offline Tesseract OCR, Arabic by default."""

    name = "tesseract"

    def __init__(self, language: str = "ara", psm: str | int = "auto",
                 config_extra: str = "") -> None:
        if not _find_tesseract():
            raise RuntimeError(
                "Tesseract executable not found. Install Tesseract-OCR with "
                "Arabic data ('ara') and make sure it is on PATH, or set the "
                "TESSERACT_CMD environment variable."
            )
        self._language = language
        self._psm = psm
        self._config_extra = config_extra.strip()
        self._version: str | None = None

    # -- OCRBackend ------------------------------------------------------
    @property
    def version(self) -> str:
        if self._version is None:
            self._version = str(pytesseract.get_tesseract_version())
        return self._version

    @property
    def language(self) -> str:
        return self._language

    @property
    def psm(self) -> str | int:
        return self._psm

    def recognize_page(self, image: Image.Image,
                       page_number: int = 1) -> OCRPageResult:
        width, height = image.size
        meta: dict = {"psm_requested": str(self._psm)}

        if str(self._psm).lower() == "auto":
            words: list[OCRWord] = []
            psm_used: int | None = None
            for mode in TESSERACT_AUTO_PSM_MODES:
                words = self._recognize(image, mode)
                if words:
                    psm_used = mode
                    break
            if psm_used is None:                # nothing found with either
                psm_used = TESSERACT_AUTO_PSM_MODES[0]
                words = self._recognize(image, psm_used)
            meta["psm_used"] = psm_used
        else:
            psm_used = int(self._psm)
            words = self._recognize(image, psm_used)
            meta["psm_used"] = psm_used

        return OCRPageResult(
            page=page_number,
            image_width=width,
            image_height=height,
            words=words,
            engine=self.name,
            engine_version=self.version,
            meta=meta,
        )

    # -- helpers ---------------------------------------------------------
    def _recognize(self, image: Image.Image, psm: int) -> list[OCRWord]:
        config = f"--psm {psm}"
        if self._config_extra:
            config = f"{config} {self._config_extra}"

        data = pytesseract.image_to_data(
            image,
            lang=self._language,
            config=config,
            output_type=pytesseract.Output.DICT,
        )

        words: list[OCRWord] = []
        n = len(data["text"])
        for i in range(n):
            text = data["text"][i]
            if not text or not text.strip():
                continue                        # non-word rows carry no text
            conf = float(data["conf"][i])
            if conf < 0:
                continue                        # -1 = not a word-level result
            w = int(data["width"][i])
            h = int(data["height"][i])
            if w <= 0 or h <= 0:
                continue
            words.append(
                OCRWord(
                    text=text,
                    confidence=round(conf, 1),
                    x=int(data["left"][i]),
                    y=int(data["top"][i]),
                    width=w,
                    height=h,
                    block=int(data["block_num"][i]),
                    paragraph=int(data["par_num"][i]),
                    line=int(data["line_num"][i]),
                    word_index=int(data["word_num"][i]),
                )
            )
        return words
