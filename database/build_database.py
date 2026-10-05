#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build ``database/buhaira.db`` from ``normalized_buhaira/dictionary_normalized.csv``.

The Al-Buhaira Arabic->Malayalam dictionary, cleaned and normalized, loaded into
SQLite. This script is the ONLY thing that writes the database; it is fully
reproducible from the CSV and safe to re-run (it always rebuilds from scratch,
it never appends).

Guarantees enforced here
------------------------
* The CSV is opened read-only and is never modified.
* ``arabic`` is stored byte-for-byte exactly as supplied. It is NOT normalized
  again here, and NOT re-encoded (no NFC/NFKC pass), because the original
  fully-vocalized form is the dictionary entry identity.
* ``arabic_normalized`` is copied verbatim from the CSV. The normalization
  algorithm is NOT re-implemented, re-run, or modified here.
* No row is merged, deduplicated, or dropped. One CSV row == one database row.
  4919 in, 4919 out.
* ``arabic`` and ``arabic_normalized`` are deliberately NOT unique. A normalized
  key can legitimately map to several dictionary entries.

Usage
-----
    python database/build_database.py
    python database/build_database.py --csv path/to.csv --db path/to/out.db
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

try:  # make Arabic/Malayalam print correctly on legacy Windows consoles
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):  # pragma: no cover
    pass

# --------------------------------------------------------------------------
# paths / expectations
# --------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

DEFAULT_CSV = ROOT / "normalized_buhaira" / "dictionary_normalized.csv"
DEFAULT_REVIEW = ROOT / "normalized_buhaira" / "collision_review.csv"
DEFAULT_DB = HERE / "buhaira.db"

REQUIRED_COLUMNS = ("arabic", "arabic_normalized", "english", "malayalam")
CSV_COLUMNS = ("english", "arabic", "arabic_normalized", "malayalam")

EXPECTED_ROWS = 4919
# NOTE: was 4164 before the 2026-10-05 swapped-column repair and is now 4167.
# The repair (normalized_buhaira/repair_swapped_columns.py) rotated 340 rows whose
# arabic/malayalam/english columns were mis-assigned by extract_buh.py. Those 340
# rows held only 332 distinct values in the arabic column but hold 335 distinct
# Arabic forms after repair, and none of them collide with the other 4579 rows:
#   4164 - 332 + 335 = 4167
# The 5 repeated Arabic forms are legitimate distinct senses of one word
# (e.g. الثَّانِيَةُ = "Second (time unit)" and "Two o'clock"), which the upstream
# cleaner kept because it deduplicated on the whole (english, arabic, malayalam)
# tuple. Deduplicating them here is forbidden.
EXPECTED_UNIQUE_ARABIC = 4167
EXPECTED_UNIQUE_NORMALIZED = 3812

# Phrase rows likewise moved 1338 -> 1313: 293 of the swapped rows were flagged as
# phrases only because the "arabic" column held multi-word Malayalam; just 268 of
# them are genuinely multi-word Arabic. The remaining 25 are correctly single words.
EXPECTED_PHRASE_ROWS = 1313

# ``collision_review.csv`` only covers groups where one normalized key is fed by
# MORE THAN ONE DISTINCT SPELLING of the Arabic (that is what normalization
# actually collapses). Groups where a single original spelling simply carries
# several senses are not normalization collisions, so the raw
# "COUNT(*) > 1" grouping is larger than this number -- see docs.
# 283 before the repair; the repair introduced 3 genuine new ones
# (الثغر, الثلاثاء, الثمانية), all classified LEXICALLY_DISTINCT.
EXPECTED_SPELLING_COLLISION_GROUPS = 286

SCHEMA = """
CREATE TABLE entries (
    id                INTEGER PRIMARY KEY,
    arabic            TEXT NOT NULL,
    arabic_normalized TEXT NOT NULL,
    english           TEXT,
    malayalam         TEXT,
    is_phrase         INTEGER NOT NULL DEFAULT 0,
    source            TEXT NOT NULL DEFAULT 'Al-Buhaira'
);
"""

INDEXES = (
    "CREATE INDEX idx_entries_arabic ON entries(arabic);",
    "CREATE INDEX idx_entries_arabic_normalized ON entries(arabic_normalized);",
    "CREATE INDEX idx_entries_is_phrase ON entries(is_phrase);",
)

INSERT_SQL = (
    "INSERT INTO entries "
    "(arabic, arabic_normalized, english, malayalam, is_phrase, source) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)

# word counting for the is_phrase flag: whitespace separated, empties ignored
_WORD_RE = re.compile(r"\s+")
_ARABIC_CHARS = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_MALAYALAM_CHARS = re.compile(r"[ഀ-ൿ]")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def word_count(text: str) -> int:
    """Number of whitespace separated words in ``text``."""
    if not text:
        return 0
    return len([w for w in _WORD_RE.split(text.strip()) if w])


def phrase_flag(arabic: str) -> int:
    """``is_phrase`` = 1 when the Arabic field holds more than one word.

    Spelling and spacing are never touched; phrases are kept whole.
    """
    return 1 if word_count(arabic) > 1 else 0


def fresh_db(db_path: Path) -> None:
    """Remove any previous database (+ sidecar files) so rebuilds are clean."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def read_csv(csv_path: Path) -> list[dict]:
    """Read the normalized dictionary CSV. Opened read-only, never written."""
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in CSV_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(
                f"FATAL: {csv_path} is missing required column(s): {missing}"
            )
        rows = [dict(r) for r in reader]

    problems = []
    for i, r in enumerate(rows, start=2):  # +2 -> 1-based, line 1 is the header
        for col in REQUIRED_COLUMNS:
            if r.get(col) is None:
                problems.append(f"line {i}: column '{col}' is NULL")
    if problems:
        raise SystemExit(
            "FATAL: structural problems in %s:\n  %s"
            % (csv_path, "\n  ".join(problems[:20]))
        )
    return rows


# --------------------------------------------------------------------------
# build steps
# --------------------------------------------------------------------------
def create_schema(conn: sqlite3.Connection) -> None:
    """Create the ``entries`` table. No UNIQUE constraint on any Arabic column."""
    conn.executescript(SCHEMA)


def insert_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Insert every CSV row, parameterized, inside a single transaction."""
    payload = [
        (
            r["arabic"],              # original Arabic, verbatim
            r["arabic_normalized"],  # normalization, verbatim from the CSV
            r["english"],
            r["malayalam"],
            phrase_flag(r["arabic"]),
            "Al-Buhaira",
        )
        for r in rows
    ]
    with conn:  # one transaction: commit on success, rollback on error
        conn.executemany(INSERT_SQL, payload)


def create_indexes(conn: sqlite3.Connection) -> None:
    """Indexes are built after loading so the insert stays fast."""
    with conn:
        conn.executescript("".join(INDEXES))


# --------------------------------------------------------------------------
# validation (spec section 8 + integrity checks)
# --------------------------------------------------------------------------
class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def check(self, ok: bool, label: str, detail: str) -> None:
        tag = "PASS" if ok else "FAIL"
        self.lines.append(f"  [{tag}] {label:<34} {detail}")
        if not ok:
            self.failures.append(f"{label}: {detail}")

    def warn(self, label: str, detail: str) -> None:
        self.warnings.append(f"{label}: {detail}")


def validate(conn: sqlite3.Connection, rows: list[dict], review_path: Path) -> Report:
    rep = Report()
    one = lambda sql, args=(): conn.execute(sql, args).fetchone()[0]

    # ---- A. total rows -------------------------------------------------
    db_rows = one("SELECT COUNT(*) FROM entries")
    rep.check(
        db_rows == EXPECTED_ROWS and db_rows == len(rows),
        "A. total rows",
        f"{db_rows} (csv {len(rows)}, expected {EXPECTED_ROWS})",
    )

    # ---- B. unique original Arabic ------------------------------------
    db_uniq_ar = one("SELECT COUNT(DISTINCT arabic) FROM entries")
    csv_uniq_ar = len({r["arabic"] for r in rows})
    rep.check(
        db_uniq_ar == EXPECTED_UNIQUE_ARABIC and db_uniq_ar == csv_uniq_ar,
        "B. unique original arabic",
        f"{db_uniq_ar} (csv {csv_uniq_ar}, expected {EXPECTED_UNIQUE_ARABIC})",
    )

    # ---- C. unique normalized Arabic ----------------------------------
    db_uniq_nm = one("SELECT COUNT(DISTINCT arabic_normalized) FROM entries")
    csv_uniq_nm = len({r["arabic_normalized"] for r in rows})
    rep.check(
        db_uniq_nm == EXPECTED_UNIQUE_NORMALIZED and db_uniq_nm == csv_uniq_nm,
        "C. unique normalized arabic",
        f"{db_uniq_nm} (csv {csv_uniq_nm}, expected {EXPECTED_UNIQUE_NORMALIZED})",
    )

    # ---- D. phrase count vs CSV ---------------------------------------
    db_phrase_rows = one("SELECT COUNT(*) FROM entries WHERE is_phrase = 1")
    csv_phrase_rows = sum(1 for r in rows if phrase_flag(r["arabic"]))
    db_phrase_words = one("SELECT COUNT(DISTINCT arabic) FROM entries WHERE is_phrase = 1")
    rep.check(
        db_phrase_rows == csv_phrase_rows == EXPECTED_PHRASE_ROWS,
        "D. phrase rows (is_phrase=1)",
        f"{db_phrase_rows} rows / {db_phrase_words} distinct forms "
        f"(csv {csv_phrase_rows}, expected {EXPECTED_PHRASE_ROWS})",
    )

    # ---- E0. column integrity: no swapped-column rows may reappear ------
    # In the source data 340 rows had Malayalam in `arabic` and the English gloss
    # in `malayalam` because extract_buh.py mis-assigned rotated HTML table cells.
    # That made them unsearchable by Arabic; this check stops it coming back.
    no_arabic = sum(1 for r in rows if not _ARABIC_CHARS.search(r["arabic"] or ""))
    ml_in_arabic = sum(1 for r in rows if _MALAYALAM_CHARS.search(r["arabic"] or ""))
    ml_missing = sum(1 for r in rows if not _MALAYALAM_CHARS.search(r["malayalam"] or ""))
    rep.check(
        no_arabic == 0 and ml_in_arabic == 0 and ml_missing == 0,
        "E0. no swapped-column rows",
        f"arabic col without Arabic={no_arabic} | malayalam in arabic col="
        f"{ml_in_arabic} | malayalam col without Malayalam={ml_missing}",
    )

    # ---- E. empty / NULL values ---------------------------------------
    null_ar = one("SELECT COUNT(*) FROM entries WHERE arabic IS NULL")
    null_nm = one("SELECT COUNT(*) FROM entries WHERE arabic_normalized IS NULL")
    emp_ar = one("SELECT COUNT(*) FROM entries WHERE TRIM(arabic) = ''")
    emp_nm = one("SELECT COUNT(*) FROM entries WHERE TRIM(arabic_normalized) = ''")
    null_en = one("SELECT COUNT(*) FROM entries WHERE english IS NULL")
    null_ml = one("SELECT COUNT(*) FROM entries WHERE malayalam IS NULL")
    rep.check(
        null_ar == 0 and null_nm == 0 and emp_ar == 0 and emp_nm == 0,
        "E1. null/empty arabic fields",
        f"arabic null={null_ar} empty={emp_ar} | normalized null={null_nm} empty={emp_nm}",
    )
    rep.check(
        null_en == 0 and null_ml == 0,
        "E2. null english/malayalam",
        f"english null={null_en} | malayalam null={null_ml}",
    )

    # NOT NULL enforced by schema?
    notnull_ar = one("SELECT COUNT(*) FROM pragma_table_info('entries') WHERE name='arabic' AND \"notnull\"=1")
    notnull_nm = one("SELECT COUNT(*) FROM pragma_table_info('entries') WHERE name='arabic_normalized' AND \"notnull\"=1")
    notnull_src = one("SELECT COUNT(*) FROM pragma_table_info('entries') WHERE name='source' AND \"notnull\"=1")
    rep.check(
        notnull_ar == 1 and notnull_nm == 1 and notnull_src == 1,
        "E3. NOT NULL declared on arabic fields",
        f"arabic={notnull_ar} normalized={notnull_nm} source={notnull_src}",
    )

    # ---- F. normalized collisions are present AND not collapsed -------
    multi_rows = one(
        "SELECT COUNT(*) FROM (SELECT 1 FROM entries "
        "GROUP BY arabic_normalized HAVING COUNT(*) > 1)"
    )
    rows_in_multi = one(
        "SELECT COALESCE(SUM(c), 0) FROM (SELECT COUNT(*) c FROM entries "
        "GROUP BY arabic_normalized HAVING COUNT(*) > 1)"
    )
    # a "spelling collision" = one normalized key fed by >1 distinct spelling
    spelling_groups = conn.execute(
        "SELECT arabic_normalized, COUNT(DISTINCT arabic) n "
        "FROM entries GROUP BY arabic_normalized HAVING n > 1"
    ).fetchall()
    db_spelling = {r["arabic_normalized"] for r in spelling_groups}

    reviewed: set[str] = set()
    if review_path.exists():
        with open(review_path, "r", encoding="utf-8-sig", newline="") as fh:
            reviewed = {r["arabic_normalized"] for r in csv.DictReader(fh)}

    rep.check(
        multi_rows > 1,
        "F1. normalized collisions exist",
        f"{multi_rows} multi-row groups ({rows_in_multi} rows involved)",
    )
    rep.check(
        len(db_spelling) == EXPECTED_SPELLING_COLLISION_GROUPS,
        "F2. spelling-collision groups",
        f"{len(db_spelling)} (expected {EXPECTED_SPELLING_COLLISION_GROUPS} "
        f"= {len(reviewed)} keys in collision_review.csv)",
    )
    if reviewed:
        only_db = db_spelling - reviewed
        only_rev = reviewed - db_spelling
        rep.check(
            not only_db and not only_rev,
            "F3. collision keys match review",
            f"in db only={len(only_db)} in review only={len(only_rev)}",
        )
        if only_db or only_rev:
            rep.lines.append(f"        db-only keys : {sorted(only_db)[:5]}")
            rep.lines.append(f"        rev-only keys: {sorted(only_rev)[:5]}")

    # every collision group must still hold all of its rows
    worst = conn.execute(
        "SELECT arabic_normalized, COUNT(*) c, COUNT(DISTINCT arabic) d "
        "FROM entries GROUP BY arabic_normalized HAVING c > 1 "
        "ORDER BY c DESC LIMIT 1"
    ).fetchone()
    if worst:
        coll_rows_in_csv = sum(
            1 for r in rows if r["arabic_normalized"] == worst["arabic_normalized"]
        )
        rep.check(
            worst["c"] == coll_rows_in_csv,
            "F4. no group collapsed",
            f"largest group '{worst['arabic_normalized']}' holds {worst['c']} rows "
            f"({worst['d']} spellings), csv has {coll_rows_in_csv}",
        )

    # ---- schema shape ---------------------------------------------------
    cols = [(r["name"], r["type"], r["notnull"]) for r in
            conn.execute("SELECT name, type, \"notnull\" FROM pragma_table_info('entries')")]
    expected_cols = [
        ("id", "INTEGER", 0),
        ("arabic", "TEXT", 1),
        ("arabic_normalized", "TEXT", 1),
        ("english", "TEXT", 0),
        ("malayalam", "TEXT", 0),
        ("is_phrase", "INTEGER", 1),
        ("source", "TEXT", 1),
    ]
    rep.check(cols == expected_cols, "G1. schema columns", str([c[0] for c in cols]))

    idx = {r["name"]: r["unique"] for r in
           conn.execute("SELECT name, \"unique\" FROM pragma_index_list('entries')")}
    want_idx = {
        "idx_entries_arabic",
        "idx_entries_arabic_normalized",
        "idx_entries_is_phrase",
    }
    rep.check(
        want_idx <= set(idx),
        "G2. indexes present",
        ", ".join(sorted(want_idx & set(idx))),
    )
    rep.check(
        not any(idx[n] for n in want_idx),
        "G3. no UNIQUE arabic index",
        "arabic and arabic_normalized are both non-unique",
    )

    # ---- H. integrity ----------------------------------------------------
    integrity = one("PRAGMA integrity_check")
    rep.check(integrity == "ok", "H1. PRAGMA integrity_check", str(integrity))

    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    rep.check(len(fk) == 0, "H2. PRAGMA foreign_key_check", f"{len(fk)} violations")

    # ---- row-for-row fidelity against the CSV ---------------------------
    mismatch = 0
    for r in rows:
        got = conn.execute(
            "SELECT 1 FROM entries WHERE arabic = ? AND arabic_normalized = ? "
            "AND IFNULL(english,'') = ? AND IFNULL(malayalam,'') = ? "
            "AND is_phrase = ?",
            (r["arabic"], r["arabic_normalized"], r["english"], r["malayalam"],
             phrase_flag(r["arabic"])),
        ).fetchone()
        if got is None:
            mismatch += 1
    rep.check(mismatch == 0, "I1. every CSV row stored verbatim", f"{mismatch} missing")

    # source default
    srcs = {r[0] for r in conn.execute("SELECT DISTINCT source FROM entries")}
    rep.check(srcs == {"Al-Buhaira"}, "I2. source column", ", ".join(sorted(srcs)))

    return rep


# --------------------------------------------------------------------------
# upstream data-quality audit (reported, never auto-corrected)
# --------------------------------------------------------------------------
def audit_source(rows: list[dict]) -> list[str]:
    """Non-fatal observations about the CSV. Nothing here is fixed silently.

    The build is a faithful mirror of ``dictionary_normalized.csv``; these are
    reported so the app team can decide whether to fix the upstream CSV in a
    later revision.
    """
    notes = []

    swapped = [r for r in rows if _MALAYALAM_CHARS.search(r["arabic"] or "")]
    if swapped:
        notes.append(
            f"REGRESSION: {len(swapped)} row(s) have Malayalam script in the "
            f"'arabic' column again. Run "
            f"normalized_buhaira/repair_swapped_columns.py --dry-run to investigate."
        )
    else:
        notes.append(
            "no swapped-column rows: every 'arabic' value contains Arabic script "
            "and every 'malayalam' value contains Malayalam script "
            "(the 340 rows mis-assigned by extract_buh.py were repaired)."
        )

    # a handful of English glosses legitimately contain an Arabic transliteration
    eng_arabic = [r for r in rows if _ARABIC_CHARS.search(r["english"] or "")]
    if eng_arabic:
        notes.append(
            f"{len(eng_arabic)} row(s) have an Arabic word inside the English "
            f"gloss (e.g. 'Kaaba' written as الكعبة). Pre-existing and correct "
            f"- not a swapped column, left as supplied."
        )

    slash = [r for r in rows if "/" in (r["arabic"] or "")]
    if slash:
        notes.append(
            f"{len(slash)} row(s) use '/' as an alternatives separator inside the "
            f"'arabic' field; they are stored verbatim and flagged as phrases by "
            f"the multi-word rule."
        )

    non_nfc = [
        r for r in rows
        if unicodedata.normalize("NFC", r["arabic"]) != r["arabic"]
    ]
    if non_nfc:
        notes.append(
            f"{len(non_nfc)} row(s) are not NFC-normalized (differing Unicode "
            f"encodings of the same harakat). This is the cause of the "
            f"SAFE_ORTHOGRAPHIC collision groups; stored byte-for-byte as supplied."
        )

    multi_sense = defaultdict(set)
    for r in rows:
        multi_sense[r["arabic"]].add(r["english"])
    rich = sum(1 for v in multi_sense.values() if len(v) > 1)
    notes.append(
        f"{rich} Arabic form(s) carry more than one English sense "
        f"(all rows are kept -- meanings are never dropped)."
    )

    dup4 = Counter(
        (r["english"], r["arabic"], r["arabic_normalized"], r["malayalam"])
        for r in rows
    )
    extra = sum(v - 1 for v in dup4.values() if v > 1)
    if extra:
        notes.append(
            f"{extra} fully identical duplicate row(s) exist in the CSV and are "
            f"kept as separate entries (no de-duplication)."
        )

    return notes


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def build(csv_path: Path, db_path: Path, review_path: Path) -> tuple[Report, list[str]]:
    csv_path = csv_path.resolve()
    db_path = db_path.resolve()
    review_path = review_path.resolve()

    print("=" * 68)
    print("Al-Buhaira dictionary database build")
    print("=" * 68)
    print(f"source csv : {csv_path}")
    print(f"output db  : {db_path}")

    if not csv_path.exists():
        raise SystemExit(f"FATAL: source CSV not found: {csv_path}")
    csv_path_before = (csv_path.stat().st_size, csv_path.stat().st_mtime_ns)

    rows = read_csv(csv_path)
    print(f"csv rows   : {len(rows)}")

    fresh_db(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = open_db(db_path)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = FULL")
        create_schema(conn)
        print("schema     : entries created")
        insert_rows(conn, rows)
        print(f"inserted   : {len(rows)} rows in one transaction")
        create_indexes(conn)
        print("indexes    : " + ", ".join(n.strip().split()[2] for n in INDEXES))
        rep = validate(conn, rows, review_path)
    finally:
        conn.close()

    csv_path_after = (csv_path.stat().st_size, csv_path.stat().st_mtime_ns)
    rep.check(csv_path_before == csv_path_after, "J1. source csv untouched",
              "size + mtime unchanged")
    rep.check(db_path.exists(), "J2. database file written",
              f"{db_path.stat().st_size:,} bytes")

    return rep, audit_source(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="source CSV")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="database to build")
    ap.add_argument("--review", type=Path, default=DEFAULT_REVIEW,
                    help="collision_review.csv used to cross-check collision keys")
    args = ap.parse_args(argv)

    rep, audit = build(args.csv, args.db, args.review)

    print("\nVALIDATION")
    for line in rep.lines:
        print(line)

    if audit:
        print("\nSOURCE DATA OBSERVATIONS (reported, not modified)")
        for note in audit:
            print(f"  [note] {note}")

    ok = not rep.failures
    print("\n" + "=" * 68)
    if ok:
        print(f"BUILD OK  --  {args.db}")
    else:
        print(f"BUILD FAILED  --  {len(rep.failures)} check(s) failed:")
        for f in rep.failures:
            print(f"  - {f}")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())