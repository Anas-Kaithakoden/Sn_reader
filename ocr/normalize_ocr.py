# -*- coding: utf-8 -*-
"""Text preparation for OCR words -- two deliberately separate stages.

Stage A: OCR Unicode cleanup (:func:`cleanup_ocr_text`)
    Turns whatever the OCR engine emitted into ordinary, well-formed Unicode
    Arabic.  Conservative: it only removes invisible format/bidi characters
    and applies Unicode normalization (NFKC folds Arabic presentation forms
    and ligatures such as ``ﻫ`` / ``셧`` back to base letters, NFC recomposes
    combining marks).  It never deletes harakat, tatweel, punctuation or
    Quranic marks -- the raw OCR text stays available in ``text``.

Stage B: dictionary normalization (:func:`dictionary_normalize`)
    Produces the lookup key stored as ``text_normalized``.  This reuses the
    project's existing algorithm from ``normalized_buhaira/normalize_arabic.py``
    -- it is NOT reimplemented here -- after stripping stray edge punctuation
    that OCR often glues onto a word (``ذهب،`` -> ``ذهب``).

Stored per word:

======================  ==================================================
``text``                raw OCR output, untouched
``text_clean``          stage A -- normal Unicode Arabic
``text_normalized``     stage B -- dictionary lookup key
======================  ==================================================

Example::

    raw OCR        وذَﻫَﺐَ            (presentation forms)
    stage A        ذَهَبَ             (folded + composed, harakat kept)
    stage B        ذهب                (dictionary key)
"""

from __future__ import annotations

import re
import unicodedata

from normalized_buhaira.normalize_arabic import normalize_arabic

__all__ = [
    "cleanup_ocr_text",
    "dictionary_normalize",
    "strip_edge_punctuation",
    "is_arabic_word",
    "arabic_letter_count",
    "ARABIC_RE",
]

# Invisible characters OCR engines frequently emit around Arabic tokens:
# ZWSP/ZWNJ/ZWJ, LRM/RLM, Arabic Letter Mark, word joiner, BOM, and the
# bidi embedding/override/control range.
_FORMAT_CHARS = re.compile(
    "[\u200B-\u200F\u061C\u202A-\u202E\u2060-\u2064\uFEFF]"
)

# Arabic script ranges (basic + supplements + presentation forms), used to
# decide whether a token "looks Arabic".  Whether a character counts as a
# LETTER is decided by its Unicode category, so Arabic-Indic digits (٠..٩)
# and signs are never mistaken for letters.
_ARABIC_RANGES = (
    "\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF"
)
_ARABIC_CHAR = re.compile("[" + _ARABIC_RANGES + "]")
_LATIN_LETTER = re.compile("[A-Za-z]")

ARABIC_RE = re.compile(
    "[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]"
)

# Punctuation OCR likes to attach to word tokens (Latin + Arabic + quotes).
_EDGE_PUNCT = (
    ".,;:!?،؛؟«»“”‘’\"'()[]{}…<>/\\|@#$%^&*+=~`"
    "\u00AB\u00BB\u201C\u201D\u2018\u2019"
)


def cleanup_ocr_text(text: str) -> str:
    """Stage A -- conservative OCR Unicode cleanup.

    * strips invisible format/bidi characters,
    * NFKC-folds Arabic presentation forms and ligatures to base letters,
    * NFC-recomposes combining marks.

    Diacritics (harakat), tatweel and punctuation are preserved.
    """
    if not text:
        return ""
    s = str(text)
    s = _FORMAT_CHARS.sub("", s)
    s = unicodedata.normalize("NFKC", s)
    s = unicodedata.normalize("NFC", s)
    return s


def strip_edge_punctuation(text: str) -> str:
    """Remove punctuation glued to the start/end of an OCR token.

    This is OCR token cleanup, not Arabic normalization: ``ذهب،`` must be
    able to reach the dictionary as ``ذهب``.
    """
    return str(text).strip(_EDGE_PUNCT)


def dictionary_normalize(text: str) -> str:
    """Stage B -- the dictionary lookup key (``text_normalized``).

    Reuses ``normalized_buhaira.normalize_arabic.normalize_arabic`` unchanged;
    this function only decides what to hand it (stage-A output with stray edge
    punctuation removed).
    """
    if not text:
        return ""
    return normalize_arabic(strip_edge_punctuation(cleanup_ocr_text(text)))


def arabic_letter_count(text: str) -> int:
    """Number of Arabic *letters* (category L) in the token."""
    if not text:
        return 0
    return sum(
        1
        for ch in cleanup_ocr_text(text)
        if _ARABIC_CHAR.match(ch) and unicodedata.category(ch).startswith("L")
    )


def is_arabic_word(text: str) -> bool:
    """True when a token is Arabic-looking (Arabic letters >= Latin letters)."""
    if not text:
        return False
    ar = arabic_letter_count(text)
    if ar == 0:
        return False
    latin = len(_LATIN_LETTER.findall(cleanup_ocr_text(text)))
    return ar >= latin
