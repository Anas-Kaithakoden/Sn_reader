# -*- coding: utf-8 -*-
"""OCR engine abstraction.

``ocr_pdf.py`` (the pipeline) only ever talks to the :class:`OCRBackend`
interface defined here, so the OCR engine can be replaced -- locally and
offline -- without touching the pipeline, the output schema, the normalizer,
or the reader.

An engine implements::

    class MyBackend(OCRBackend):
        name = "my-engine"
        def recognize_page(self, image, page_number=1) -> OCRPageResult: ...

and is registered in ``ocr_pdf.make_backend()``.

Coordinates
-----------
Every bounding box is expressed in the coordinate system of the *rendered page
image* that was handed to :meth:`OCRBackend.recognize_page` (pixels, origin at
the top-left corner, y growing downwards).  Because the pipeline renders the
PDF at a known DPI, those pixel coordinates can be scaled directly onto the
PDF.js page in ``reader.html``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class OCRWord:
    """One word detected by the OCR engine.

    ``text`` is exactly what the engine returned -- never reversed, never
    stripped of diacritics.  Normalization happens later, in the pipeline.
    """

    text: str
    confidence: float
    x: float
    y: float
    width: float
    height: float
    block: int | None = None
    paragraph: int | None = None
    line: int | None = None
    word_index: int | None = None

    @property
    def bbox(self) -> dict:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class OCRPageResult:
    """Recognition output for a single rendered page."""

    page: int
    image_width: int
    image_height: int
    words: list[OCRWord] = field(default_factory=list)
    engine: str = ""
    engine_version: str = ""
    meta: dict = field(default_factory=dict)


class OCRBackend(abc.ABC):
    """Interface every OCR engine must satisfy."""

    #: short identifier written into ``manifest.json`` (e.g. ``"tesseract"``)
    name: str = "unknown"

    @property
    @abc.abstractmethod
    def version(self) -> str:
        """Engine version string, written into ``manifest.json``."""

    @property
    @abc.abstractmethod
    def language(self) -> str:
        """Language code(s) used for recognition, e.g. ``"ara"``."""

    @abc.abstractmethod
    def recognize_page(self, image, page_number: int = 1) -> OCRPageResult:
        """Recognize one rendered page image and return word-level results.

        Args:
            image: a ``PIL.Image.Image`` in RGB, at the pipeline DPI.
            page_number: 1-based page number (engines that do not need it
                may ignore it).
        """
