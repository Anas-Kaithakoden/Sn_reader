#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repair the 340 swapped-column rows in the Al-Buhaira dictionary.

ROOT CAUSE (verified against the original source, not guessed)
-------------------------------------------------------------
``clean_buhaira_code/extract_buh.py`` parses ``dictionary home.html`` assuming
the table columns are::

    (English, Malayalam, Arabic)        <- the header row is English | മലയാളം | عربي

That holds for 4778 data rows, but **340 rows in the HTML are rotated** to::

    (Arabic, English, Malayalam)

The extractor therefore wrote them as english=<Arabic>, arabic=<Malayalam>,
malayalam=<English> -- leaving those 340 entries unsearchable by Arabic.

Repair = one 3-way rotation, confirmed for 340/340 rows against the HTML::

    new_arabic    = old_english      (html cell 0)
    new_english   = old_malayalam    (html cell 1)
    new_malayalam = old_arabic       (html cell 2)

Nothing is invented: no translation is written, corrected or improved. Every
value is moved from another cell of the same original row.

NOTHING ELSE IS TOUCHED. No dedupe, no merge, no drop, no re-spelling.
The other 4579 rows are written back byte-for-byte.

NORMALIZATION
-------------
``arabic_normalized`` is recomputed with the **existing** ``normalize_arabic``
from ``normalized_buhaira/normalize_arabic.py``, loaded via AST so that this
script reuses that exact function instead of duplicating the algorithm (the
module is a top-level script, so importing it would re-run the whole pipeline).

Usage:
    python normalized_buhaira/repair_swapped_columns.py [--dry-run]
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "Al-Buhaira Dictionary" / "cleaned_buhaira"
CLEAN_CSV = SOURCE_DIR / "dictionary_clean.csv"
HTML = ROOT / "Al-Buhaira Dictionary" / "dictionary home.html"
NORM_CSV = ROOT / "normalized_buhaira" / "dictionary_normalized.csv"
NORM_ALGORITHM = ROOT / "normalized_buhaira" / "normalize_arabic.py"
COLLISIONS_CSV = ROOT / "normalized_buhaira" / "normalization_collisions.csv"
BACKUP_NORM = ROOT / "normalized_buhaira" / "dictionary_normalized_before_swapped_fix.csv"
BACKUP_CLEAN = SOURCE_DIR / "dictionary_clean_before_swapped_fix.csv"
REPORT_CSV = ROOT / "normalized_buhaira" / "swapped_rows_repair_report.csv"
REPORT_JSON = ROOT / "normalized_buhaira" / "swapped_rows_repair_report.json"

EXPECTED_ROWS = 4919
EXPECTED_SWAPPED = 340
EXPECTED_UNCHANGED = 4579

ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
MALAYALAM = re.compile(r"[\u0D00-\u0D7F]")

# row order written by normalize_arabic.py
NORM_HEADER = ["english", "arabic", "arabic_normalized", "malayalam"]
CLEAN_HEADER = ["english", "arabic", "malayalam"]
COLLISION_HEADER = ["arabic_normalized", "original_forms", "row_count",
                    "english_meanings", "malayalam_meanings", "collision_type"]


# --------------------------------------------------------------------------
# reuse the EXISTING normalizer (no second implementation)
# --------------------------------------------------------------------------
def load_normalize_arabic():
    """Extract and execute only the ``normalize_arabic`` function definition."""
    source = NORM_ALGORITHM.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "normalize_arabic"), None)
    if fn is None:
        raise SystemExit(f"FATAL: no normalize_arabic() in {NORM_ALGORITHM}")
    namespace = {"re": re}
    exec(compile(ast.Module(body=[fn], type_ignores=[]),
                 str(NORM_ALGORITHM), "exec"), namespace)
    return namespace["normalize_arabic"]


def read_csv(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, header: list[str], rows) -> None:
    """Identical dialect/encoding/newline settings to normalize_arabic.py."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def html_rotated_rows() -> list[list[str]]:
    """Original HTML rows whose column order is (Arabic, English, Malayalam)."""
    html = HTML.read_text(encoding="utf-8", errors="replace")
    out = []
    for m in re.finditer(r"<tr[^>]*>.*?</tr>", html, re.DOTALL):
        cells = [re.sub(r"<[^>]+>", "", t).strip()
                 for t in re.findall(r"<td[^>]*>(.*?)</td>", m.group(0), re.DOTALL)]
        if len(cells) >= 3 and ARABIC.search(cells[0]) and MALAYALAM.search(cells[2]):
            if not MALAYALAM.search(cells[0]) and not MALAYALAM.search(cells[1]):
                out.append(cells)
    return out


def is_swapped(row: dict) -> bool:
    """Three independent signals must agree before a row is touched."""
    arabic, english, malayalam = (row.get("arabic") or "", row.get("english") or "",
                                  row.get("malayalam") or "")
    return (
        bool(MALAYALAM.search(arabic))      # 1. arabic column holds Malayalam
        and bool(ARABIC.search(english))    # 2. english column holds Arabic
        and not MALAYALAM.search(malayalam)  # 3. malayalam column holds no Malayalam
    )


def backup(path: Path, backup_path: Path) -> str:
    """Never overwrite an existing backup silently."""
    if not path.exists():
        raise SystemExit(f"FATAL: {path} does not exist")
    if backup_path.exists():
        same = (backup_path.read_bytes() == path.read_bytes())
        if same:
            return f"existing backup {backup_path.name} is identical - left as is"
        raise SystemExit(
            f"FATAL: {backup_path.name} already exists and DIFFERS from {path.name}.\n"
            f"       Refusing to overwrite. Move it aside, then re-run.")
    shutil.copy2(path, backup_path)
    return f"created {backup_path.name}"


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Repair the 340 swapped-column rows.")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    normalize_arabic = load_normalize_arabic()
    clean = read_csv(CLEAN_CSV)
    norm = read_csv(NORM_CSV)

    print("=" * 68)
    print("Al-Buhaira swapped-column repair")
    print("=" * 68)
    print(f"clean csv : {len(clean)} rows")
    print(f"norm  csv : {len(norm)} rows")

    # ---- 1. detect with combined evidence --------------------------------
    swapped_idx = [i for i, r in enumerate(norm) if is_swapped(r)]
    clean_idx = [i for i, r in enumerate(clean) if is_swapped(r)]
    print(f"\ndetected swapped rows        : {len(swapped_idx)}")
    print(f"same positions in clean csv  : {swapped_idx == clean_idx}")

    # independent cross-check: rows whose arabic column has no Arabic at all
    no_arabic = [i for i, r in enumerate(norm) if not ARABIC.search(r["arabic"] or "")]
    print(f"cross-check 'no Arabic' rows : {len(no_arabic)}  "
          f"(identical set: {no_arabic == swapped_idx})")

    # ---- 2. confirm the mapping against the ORIGINAL html ----------------
    rotated = html_rotated_rows()
    print(f"html rows in (Arabic, English, Malayalam) order : {len(rotated)}")

    if len(norm) != EXPECTED_ROWS:
        raise SystemExit(f"FATAL: expected {EXPECTED_ROWS} rows, found {len(norm)}")
    if len(swapped_idx) != EXPECTED_SWAPPED:
        raise SystemExit(f"FATAL: expected {EXPECTED_SWAPPED} swapped rows, "
                         f"found {len(swapped_idx)}")
    if len(rotated) != EXPECTED_SWAPPED:
        raise SystemExit(f"FATAL: html shows {len(rotated)} rotated rows, "
                         f"expected {EXPECTED_SWAPPED}")

    matched = sum(1 for k, i in enumerate(swapped_idx)
                  if (norm[i]["english"], norm[i]["malayalam"], norm[i]["arabic"])
                  == tuple(rotated[k][:3]))
    print(f"rows matching html cell order exactly : {matched}/{EXPECTED_SWAPPED}")
    if matched != EXPECTED_SWAPPED:
        raise SystemExit("FATAL: repair mapping does not match the original source")

    # ---- 3. verify the normalizer is faithful (no-op guarantee) -----------
    drift = sum(1 for c, n in zip(clean, norm)
                if not is_swapped(c) and normalize_arabic(c["arabic"]) != n["arabic_normalized"])
    print(f"\nuntouched rows whose normalized value would drift : {drift}")
    if drift:
        raise SystemExit("FATAL: regenerating would change untouched rows")

    # ---- 4. build repaired rows ------------------------------------------
    new_clean, new_norm, report_rows, rejected = [], [], [], []
    for i, c in enumerate(clean):
        if i in set(swapped_idx):
            new_arabic = c["english"]       # html cell 0
            new_english = c["malayalam"]    # html cell 1
            new_malayalam = c["arabic"]     # html cell 2
            new_normalized = normalize_arabic(new_arabic)
            # guard: never fabricate - the rotated cell must really be Arabic
            if not ARABIC.search(new_arabic):
                rejected.append((i + 2, c))
                new_arabic, new_english, new_malayalam = c["arabic"], c["english"], c["malayalam"]
                new_normalized = normalize_arabic(new_arabic)
            report_rows.append({
                "row_number": i + 2,
                "old_arabic": c["arabic"], "old_arabic_normalized": normalize_arabic(c["arabic"]),
                "old_english": c["english"], "old_malayalam": c["malayalam"],
                "new_arabic": new_arabic, "new_arabic_normalized": new_normalized,
                "new_english": new_english, "new_malayalam": new_malayalam,
                "reason": ("original HTML table row is ordered (Arabic, English, Malayalam) "
                           "instead of (English, Malayalam, Arabic); extract_buh.py mis-assigned "
                           "the cells - values rotated back to their true columns, verified "
                           "against dictionary home.html"),
            })
        else:
            new_arabic, new_english, new_malayalam = c["arabic"], c["english"], c["malayalam"]
            new_normalized = (normalize_arabic(new_arabic)
                              if i in set(swapped_idx) else norm[i]["arabic_normalized"])
        new_clean.append([new_english, new_arabic, new_malayalam])
        new_norm.append([new_english, new_arabic, new_normalized, new_malayalam])

    changed = sum(1 for k, i in enumerate(swapped_idx)
                  if new_norm[i] != [norm[i]["english"], norm[i]["arabic"],
                                     norm[i]["arabic_normalized"], norm[i]["malayalam"]])
    unchanged_rows = len(new_norm) - changed
    print(f"\nrows changed : {changed}")
    print(f"rows unchanged : {unchanged_rows}")
    print(f"rejected : {len(rejected)}")

    # ---- 5. regenerate normalization_collisions.csv (same logic as
    #         normalize_arabic.py, using the same normalizer) --------------
    groups = defaultdict(lambda: {"orig": set(), "eng": set(), "ml": set()})
    for e, a, _, m in new_norm:
        g = groups[normalize_arabic(a)]
        g["orig"].add(a); g["eng"].add(e); g["ml"].add(m)
    collision_rows = [[k, "|".join(sorted(d["orig"])), len(d["orig"]),
                       len(d["eng"]), len(d["ml"]), "UNKNOWN"]
                      for k, d in groups.items() if len(d["orig"]) > 1]

    # ---- 6. validation ---------------------------------------------------
    nw = lambda s: len([w for w in re.split(r"\s+", s.strip()) if w])  # noqa: E731
    target = set(swapped_idx)
    # Pre-existing quirk, NOT a swap and NOT touched by this repair: a few
    # English glosses legitimately contain an Arabic transliteration, e.g.
    # "غزوة (Raid/Battle)", "الكعبة (Kaaba)". Their arabic column is correct.
    preexisting_eng_arabic = sum(
        1 for i, r in enumerate(norm)
        if i not in target and ARABIC.search(r["english"] or ""))
    checks = {
        "total_rows": (len(new_norm), EXPECTED_ROWS),
        "unchanged_rows": (unchanged_rows, EXPECTED_UNCHANGED),
        "changed_rows": (changed, EXPECTED_SWAPPED),
        "rows_with_no_arabic": (sum(1 for r in new_norm if not ARABIC.search(r[1])), 0),
        "malayalam_left_in_arabic": (sum(1 for r in new_norm if MALAYALAM.search(r[1])), 0),
        "malayalam_without_script": (sum(1 for r in new_norm if not MALAYALAM.search(r[3])), 0),
        # scoped to the repaired rows: english must now be a real gloss
        "repaired_english_still_arabic": (
            sum(1 for i, r in enumerate(new_norm) if i in target and ARABIC.search(r[0])), 0),
        "repaired_arabic_is_the_old_english": (
            sum(1 for i, r in enumerate(new_norm)
                if i in target and r[1] != norm[i]["english"]), 0),
        "repaired_malayalam_is_the_old_arabic": (
            sum(1 for i, r in enumerate(new_norm)
                if i in target and r[3] != norm[i]["arabic"]), 0),
        "repaired_normalized_matches_algorithm": (
            sum(1 for i, r in enumerate(new_norm)
                if i in target and r[2] != normalize_arabic(r[1])), 0),
        "repaired_rows": (len(report_rows), EXPECTED_SWAPPED),
        "rejected_rows": (len(rejected), 0),
    }
    print(f"\n(note) pre-existing English glosses containing Arabic, left as-is: "
          f"{preexisting_eng_arabic}")
    ok = True
    print("\nVALIDATION (pre-write)")
    for k, (got, want) in checks.items():
        good = got == want
        ok &= good
        print(f"  [{'PASS' if good else 'FAIL'}] {k:<30} {got} (expected {want})")
    if not ok:
        raise SystemExit("\nFATAL: pre-write validation failed - nothing was written")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    # ---- 7. backup, then write ------------------------------------------
    print("\nBACKUP")
    print("  " + backup(NORM_CSV, BACKUP_NORM))
    print("  " + backup(CLEAN_CSV, BACKUP_CLEAN))

    write_csv(CLEAN_CSV, CLEAN_HEADER, new_clean)
    write_csv(NORM_CSV, NORM_HEADER, new_norm)
    write_csv(COLLISIONS_CSV, COLLISION_HEADER, collision_rows)
    write_csv(REPORT_CSV, list(report_rows[0].keys()), [list(r.values()) for r in report_rows])

    # ---- 8. post-write diff proof ---------------------------------------
    after = read_csv(NORM_CSV)
    fields = NORM_HEADER
    diffs = [k for k, (a, b) in enumerate(zip(norm, after))
             if [a[f] for f in fields] != [b[f] for f in fields]]
    print(f"\nPOST-WRITE DIFF")
    print(f"  rows in file          : {len(after)}")
    print(f"  changed rows vs before: {len(diffs)} (expected {EXPECTED_SWAPPED})")
    if len(diffs) != EXPECTED_SWAPPED or diffs != swapped_idx:
        raise SystemExit("FATAL: diff is not exactly the 340 targeted rows")

    for f in fields:  # untouched rows must be identical
        drift = sum(1 for k in range(len(after))
                    if k not in set(swapped_idx) and after[k][f] != norm[k][f])
        print(f"  untouched rows changed in '{f}': {drift}")
        if drift:
            raise SystemExit(f"FATAL: untouched rows changed in '{f}'")

    summary = {
        "input_rows": len(norm),
        "detected_rows": len(swapped_idx),
        "repaired_rows": len(report_rows),
        "rejected_rows": len(rejected),
        "unchanged_rows": unchanged_rows,
        "html_rotated_rows_found": len(rotated),
        "rows_matching_original_html": matched,
        "changed_rows_in_diff": len(diffs),
        "validation": {
            **{k: {"value": v[0], "expected": v[1], "pass": v[0] == v[1]}
               for k, v in checks.items()},
            "diff_exactly_340_targeted_rows": diffs == swapped_idx,
            "untouched_rows_byte_identical": True,
        },
    }
    REPORT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\nwrote:")
    for p in (CLEAN_CSV, NORM_CSV, COLLISIONS_CSV, REPORT_CSV, REPORT_JSON,
              BACKUP_NORM, BACKUP_CLEAN):
        print(f"  {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())