#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lookup layer for ``database/buhaira.db`` (Al-Buhaira Arabic->Malayalam).

Two lookup modes, in strict priority order
------------------------------------------
1. EXACT  -- ``arabic = ?``          the dictionary entry identity.
2. NORMALIZED FALLBACK -- ``arabic_normalized = ?``  only when step 1 misses.

The normalized fallback ALWAYS returns every candidate. ``arabic_normalized`` is
not a unique key: 283 of its values are fed by more than one Arabic spelling,
and 691 of its values are shared by more than one dictionary row (some Arabic
forms carry several senses). Nothing in this module ever picks a winner. There
is deliberately no ``LIMIT 1`` anywhere -- disambiguation is the application
layer's job, using surrounding text.

    from database.lookup import lookup

    r = lookup("ذَهَبَ")                 # exact hit: 3 senses of ذَهَبَ
    r = lookup("ذهب", "ذهب")            # fallback: ذَهَب + ذَهَبَ (2 spellings)
    r = lookup("ملف", "ملف")             # nothing -> r.found is False
    r.forms                             # distinct source spellings, for display
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Entry",
    "LookupResult",
    "DEFAULT_DB_PATH",
    "connect",
    "lookup_exact",
    "lookup_normalized",
    "lookup",
    "close",
]

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "buhaira.db"

_COLUMNS = "id, arabic, arabic_normalized, english, malayalam, is_phrase, source"

# No LIMIT anywhere in this module. All candidates are always returned.
_SQL_EXACT = f"SELECT {_COLUMNS} FROM entries WHERE arabic = ? ORDER BY id"
_SQL_NORMALIZED = (
    f"SELECT {_COLUMNS} FROM entries WHERE arabic_normalized = ? ORDER BY id"
)


# --------------------------------------------------------------------------
# result types
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    """One dictionary entry. ``arabic`` is the original fully-vocalized form."""

    id: int
    arabic: str
    arabic_normalized: str
    english: str | None
    malayalam: str | None
    is_phrase: int
    source: str

    @property
    def is_phrase_flag(self) -> bool:
        return bool(self.is_phrase)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "arabic": self.arabic,
            "arabic_normalized": self.arabic_normalized,
            "english": self.english,
            "malayalam": self.malayalam,
            "is_phrase": self.is_phrase,
            "source": self.source,
        }


@dataclass(frozen=True)
class LookupResult:
    """Outcome of a lookup. Empty results are normal, not an error."""

    query: str
    match_type: str  # "exact" | "normalized"
    entries: tuple[Entry, ...] = ()
    note: str = ""

    @property
    def found(self) -> bool:
        return bool(self.entries)

    @property
    def candidate_count(self) -> int:
        """Number of returned entries. > 1 means the caller must disambiguate."""
        return len(self.entries)

    @property
    def is_ambiguous(self) -> bool:
        return len(self.entries) > 1

    @property
    def distinct_spellings(self) -> tuple[str, ...]:
        """Distinct original Arabic spellings behind this result, in order.

        More than one here means the normalized key collided with another word
        and the application layer has to choose using context.
        """
        seen: list[str] = []
        for e in self.entries:
            if e.arabic not in seen:
                seen.append(e.arabic)
        return tuple(seen)

    # `forms` reads better at call sites; keep it as an alias.
    @property
    def forms(self) -> tuple[str, ...]:
        return self.distinct_spellings

    def as_dicts(self) -> list[dict]:
        return [e.as_dict() for e in self.entries]

    def first(self) -> Entry | None:
        """First entry, for callers that only need a preview.

        This is a convenience for display, NOT a disambiguation decision --
        ambiguous results still expose every entry via ``entries``.
        """
        return self.entries[0] if self.entries else None

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    def __bool__(self) -> bool:
        return self.found


# --------------------------------------------------------------------------
# connection handling (read-only, so lookups can never corrupt the database)
# --------------------------------------------------------------------------
_connection: sqlite3.Connection | None = None
_connection_path: Path | None = None


def resolve_db_path(db_path: str | os.PathLike | None = None) -> Path:
    if db_path is not None:
        return Path(db_path).resolve()
    env = os.environ.get("BUHAIRA_DB")
    return Path(env).resolve() if env else DEFAULT_DB_PATH


def connect(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """Open (and cache) a read-only connection to the dictionary database."""
    global _connection, _connection_path

    path = resolve_db_path(db_path)
    if _connection is not None and _connection_path == path:
        return _connection

    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found -- run: python database/build_database.py"
        )

    # mode=ro: SQLite refuses to write, and refuses to create a missing file.
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    close()
    _connection, _connection_path = conn, path
    return conn


def close() -> None:
    """Close the cached connection."""
    global _connection, _connection_path
    if _connection is not None:
        _connection.close()
        _connection, _connection_path = None, None


def _rows(sql: str, value: str, match_type: str, query: str) -> LookupResult:
    if value is None or not str(value).strip():
        return LookupResult(query, match_type, (), note="empty query")
    cur = connect().execute(sql, (value,))
    entries = tuple(Entry(**dict(r)) for r in cur.fetchall())
    return LookupResult(query, match_type, entries)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def lookup_exact(arabic: str, db_path: str | os.PathLike | None = None) -> LookupResult:
    """Exact lookup on the original Arabic. May return several senses."""
    res = _rows(_SQL_EXACT, arabic, "exact", arabic)
    if res.found:
        note = (
            "exact match: the original fully-vocalized form is the entry identity"
            + ("; normalized fallback deliberately NOT used"
               if res.is_ambiguous else "")
        )
        return LookupResult(res.query, res.match_type, res.entries, note)
    return res


def lookup_normalized(
    arabic_normalized: str, db_path: str | os.PathLike | None = None
) -> LookupResult:
    """Fallback lookup on the normalized key. Returns ALL candidates."""
    res = _rows(_SQL_NORMALIZED, arabic_normalized, "normalized", arabic_normalized)
    if res.found and res.is_ambiguous:
        return LookupResult(
            res.query,
            res.match_type,
            res.entries,
            "normalized key is NOT unique -- "
            f"{res.candidate_count} candidate(s) across "
            f"{len(res.distinct_spellings)} spelling(s); all returned, "
            "caller must disambiguate with context",
        )
    if res.found:
        return LookupResult(res.query, res.match_type, res.entries,
                            "single normalized candidate")
    return LookupResult(res.query, res.match_type, res.entries, "no candidate")


def lookup(
    arabic: str,
    arabic_normalized: str | None = None,
    *,
    normalizer=None,
    db_path: str | os.PathLike | None = None,
) -> LookupResult:
    """Exact first, normalized fallback second. Never picks a single winner.

    Args:
        arabic: the original Arabic as typed/segmented.
        arabic_normalized: its already-normalized form. Required for the
            fallback whenever the exact lookup misses -- this module does not
            re-implement the normalization algorithm, it only stores what the
            CSV already contains.
        normalizer: optional ``callable(str) -> str``, used only when
            ``arabic_normalized`` is omitted, e.g. the app's own normalizer.
    """
    exact = lookup_exact(arabic, db_path)
    if exact.found:
        return exact

    key = arabic_normalized
    if key is None and normalizer is not None:
        key = normalizer(arabic)

    if key is None:
        return LookupResult(
            arabic,
            "normalized",
            (),
            note=("no exact match for this Arabic and no normalized key was "
                  "supplied; pass arabic_normalized= or normalizer= to run the "
                  "normalized fallback"),
        )

    return lookup_normalized(key, db_path)


if __name__ == "__main__":  # tiny manual smoke check
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    for query, norm in (("ذَهَبَ", "ذهب"), ("ذهب", "ذهب"), ("ملف", "ملف")):
        r = lookup(query, norm)
        print(f"{query!r} -> {r.match_type}, {r.candidate_count} candidate(s)")
        for e in r.entries:
            print(f"    {e.arabic}  |  {e.english}  |  {e.malayalam}")
    close()