#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for ``database/buhaira.db`` and ``database/lookup.py``.

Run:
    python database/test_database.py            # verbose, prints the Arabic
    python -m unittest discover -s database -v

The suite FAILS LOUDLY whenever a known normalization collision comes back with
a single candidate, because that would mean a dictionary meaning had been
silently collapsed somewhere in the pipeline. Beyond the hand-written cases it
also sweeps every reviewed collision and every multi-row group in bulk.

Test fixtures are read from the authoritative CSV rather than hand-typed, for
one concrete reason: 473 stored Arabic forms are not NFC-normalized (e.g. the
Form II verb كَتَّبَ is stored as shadda-then-fatha, U+0651 U+064E, while most
editors and keyboards produce U+064C). Visually identical literals therefore do
not always compare equal. Assertions that involve spelling compare NFC-folded
strings so they test meaning, not codepoint order; a dedicated test pins the
codepoint behaviour itself.
"""

from __future__ import annotations

import csv
import re
import sqlite3
import sys
import unicodedata
import unittest
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):  # so both `database.lookup` and `lookup` import
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from database.lookup import (
        DEFAULT_DB_PATH, Entry, close, connect, lookup, lookup_exact, lookup_normalized,
    )
except ImportError:  # pragma: no cover
    from lookup import (  # type: ignore
        DEFAULT_DB_PATH, Entry, close, connect, lookup, lookup_exact, lookup_normalized,
    )

CSV_PATH = ROOT / "normalized_buhaira" / "dictionary_normalized.csv"
REVIEW_PATH = ROOT / "normalized_buhaira" / "collision_review.csv"

EXPECTED_ROWS = 4919
# 4164 -> 4167 after the 340-row swapped-column repair (see build_database.py for
# the arithmetic). 3812 normalized keys are unchanged by the repair.
EXPECTED_UNIQUE_ARABIC = 4167
EXPECTED_UNIQUE_NORMALIZED = 3812
EXPECTED_PHRASE_ROWS = 1313  # 1338 -> 1313, see build_database.py
EXPECTED_SPELLING_COLLISION_GROUPS = 286  # 283 -> 286 (3 new, all LEXICALLY_DISTINCT)

# Normalized keys carry no harakat, so these literals are NFC-stable.
KEY_ZAHA = "ذهب"    # ذَهَبَ "to go"  vs  ذَهَب "gold"
KEY_AMAL = "عمل"    # عَمِلَ "to work" vs  عَمَل "work" (noun)
KEY_MAN = "من"      # مَنْ "who?"       vs  مِنْ "from"
KEY_KATABA = "كتب"  # كَتَبَ Form I    vs  كَتَّبَ Form II
PHRASE_KEY = "طرق الباب"  # طَرَقَ البَابَ "to knock (at the door)"

# Spellings for display and for intent. Compared NFC-folded, see module docstring.
ZAHABA_FULL, ZAHA_NOM = "ذَهَبَ", "ذَهَب"
AMAL_MAJI, AMAL_NOUN = "عَمِلَ", "عَمَل"
MAN, MIN = "مَنْ", "مِنْ"
KATABA, KATTABA = "كَتَبَ", "كَتَّبَ"
PHRASE_ARABIC = "طَرَقَ البَابَ"
ABSENT_WORDS = ("شبابيك", "زقزق")

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
_ML_RE = re.compile(r"[\u0D00-\u0D7F]")

# Entries from the 340 rows that extract_buh.py mis-assigned. Their columns were
# rotated (arabic<-english, english<-malayalam, malayalam<-arabic); these are the
# values that now sit in the correct columns and must be searchable by Arabic.
REPAIRED_ENTRIES = (
    ("الثَّوْرَةُ", "الثورة", "Revolution / Uprising", "വിപ്ലവം / പ്രക്ഷോഭം"),
    ("والثَّقَافَاتُ", "والثقافات", "Cultures", "സംസ്കാരങ്ങൾ"),
    ("الثَّلْجُ اللَّيْلِيُّ الكَثِيفُ", "الثلج الليلي الكثيف",
     "Dense night snow", "രാത്രിയിലെ കട്ടിയുള്ള മഞ്ഞ്"),
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


@lru_cache(maxsize=1)
def _csv_rows() -> tuple[dict, ...]:
    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as fh:
        return tuple(dict(r) for r in csv.DictReader(fh))


@lru_cache(maxsize=1)
def _reviewed_keys() -> tuple[str, ...]:
    with open(REVIEW_PATH, "r", encoding="utf-8-sig", newline="") as fh:
        return tuple(r["arabic_normalized"] for r in csv.DictReader(fh))


def csv_spellings(normalized_key: str) -> tuple[str, ...]:
    """Distinct original Arabic spellings the CSV stores under a normalized key."""
    return tuple(dict.fromkeys(
        r["arabic"] for r in _csv_rows() if r["arabic_normalized"] == normalized_key))


def csv_rows_for(normalized_key: str) -> list[dict]:
    return [r for r in _csv_rows() if r["arabic_normalized"] == normalized_key]


def show(label: str, result) -> None:
    print(f"\n  {label}  [{result.match_type}] {result.candidate_count} candidate(s)"
          f" / {len(result.distinct_spellings)} spelling(s)")
    for e in result.entries:
        print(f"      id={e.id:<5} {e.arabic}{'  [phrase]' if e.is_phrase else ''}")
        print(f"            EN: {e.english}")
        print(f"            ML: {e.malayalam}")


def db_rows() -> int:
    return connect().execute("SELECT COUNT(*) FROM entries").fetchone()[0]


# ==========================================================================
class TestDatabaseContents(unittest.TestCase):
    """Build-spec section 8: the numbers must match the CSV exactly."""

    @classmethod
    def setUpClass(cls):
        cls.conn = connect()
        cls.csv_rows = _csv_rows()

    def test_database_exists(self):
        self.assertTrue(Path(DEFAULT_DB_PATH).exists(), f"{DEFAULT_DB_PATH} missing")

    def test_A_total_rows(self):
        self.assertEqual(db_rows(), EXPECTED_ROWS)
        self.assertEqual(db_rows(), len(self.csv_rows))

    def test_B_unique_original_arabic(self):
        got = self.conn.execute("SELECT COUNT(DISTINCT arabic) FROM entries").fetchone()[0]
        self.assertEqual(got, EXPECTED_UNIQUE_ARABIC)
        self.assertEqual(got, len({r["arabic"] for r in self.csv_rows}))

    def test_C_unique_normalized_arabic(self):
        got = self.conn.execute(
            "SELECT COUNT(DISTINCT arabic_normalized) FROM entries").fetchone()[0]
        self.assertEqual(got, EXPECTED_UNIQUE_NORMALIZED)
        self.assertEqual(got, len({r["arabic_normalized"] for r in self.csv_rows}))

    def test_D_phrase_count_matches_csv(self):
        import re
        nw = lambda s: len([w for w in re.split(r"\s+", s.strip()) if w])  # noqa: E731
        expected = sum(1 for r in self.csv_rows if nw(r["arabic"]) > 1)
        got = self.conn.execute(
            "SELECT COUNT(*) FROM entries WHERE is_phrase = 1").fetchone()[0]
        self.assertEqual(got, expected)
        self.assertGreater(got, 0)
        mislabelled = self.conn.execute(
            "SELECT COUNT(*) FROM entries WHERE is_phrase = 0 "
            "AND TRIM(arabic) LIKE '% %'").fetchone()[0]
        self.assertEqual(mislabelled, 0, "single-word rows must not be flagged as phrases")

    def test_E_no_null_or_empty_required_fields(self):
        for col in ("arabic", "arabic_normalized"):
            n = self.conn.execute(
                f"SELECT COUNT(*) FROM entries "
                f"WHERE {col} IS NULL OR TRIM({col}) = ''").fetchone()[0]
            self.assertEqual(n, 0, f"{col} has {n} null/empty value(s)")

    def test_E_notnull_constraints_declared(self):
        info = {r["name"]: r["notnull"] for r in self.conn.execute(
            "SELECT name, \"notnull\" FROM pragma_table_info('entries')")}
        self.assertEqual(list(info), ["id", "arabic", "arabic_normalized",
                                      "english", "malayalam", "is_phrase", "source"])
        for col in ("arabic", "arabic_normalized", "is_phrase", "source"):
            self.assertEqual(info[col], 1, f"{col} must be NOT NULL")

    def test_F_collisions_exist_and_are_not_collapsed(self):
        groups = self.conn.execute(
            "SELECT arabic_normalized, COUNT(*) c FROM entries "
            "GROUP BY arabic_normalized HAVING c > 1").fetchall()
        self.assertGreater(len(groups), 1, "expected normalized collisions to exist")

        db_spelling = {
            r["arabic_normalized"] for r in self.conn.execute(
                "SELECT arabic_normalized FROM entries "
                "GROUP BY arabic_normalized HAVING COUNT(DISTINCT arabic) > 1")}
        self.assertEqual(len(db_spelling), EXPECTED_SPELLING_COLLISION_GROUPS)
        self.assertEqual(db_spelling, set(_reviewed_keys()),
                         "reviewed collision keys drifted from the database")

        csv_counts: dict[str, int] = {}
        for r in self.csv_rows:
            csv_counts[r["arabic_normalized"]] = csv_counts.get(r["arabic_normalized"], 0) + 1
        for g in groups:
            self.assertEqual(g["c"], csv_counts[g["arabic_normalized"]],
                             f"group '{g['arabic_normalized']}' lost rows")

    def test_schema_has_no_unique_arabic_constraint(self):
        idx = {r["name"]: r["unique"] for r in self.conn.execute(
            "SELECT name, \"unique\" FROM pragma_index_list('entries')")}
        for required in ("idx_entries_arabic", "idx_entries_arabic_normalized",
                         "idx_entries_is_phrase"):
            self.assertIn(required, idx)
        unique = {n for n, u in idx.items() if u}
        self.assertNotIn("arabic", unique)
        self.assertNotIn("arabic_normalized", unique)

    def test_arabic_and_normalized_stored_verbatim(self):
        """No re-normalization and no re-encoding at insert time."""
        checked = 0
        for r in self.csv_rows:
            row = self.conn.execute(
                "SELECT arabic_normalized, is_phrase FROM entries WHERE arabic = ?",
                (r["arabic"],)).fetchone()
            self.assertIsNotNone(row, f"CSV spelling missing from db: {r['arabic']!r}")
            self.assertEqual(row["arabic_normalized"], r["arabic_normalized"])
            checked += 1
        self.assertEqual(checked, EXPECTED_ROWS)

    def test_integrity_and_foreign_keys(self):
        self.assertEqual(
            self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_source_column_default(self):
        vals = {r[0] for r in self.conn.execute("SELECT DISTINCT source FROM entries")}
        self.assertEqual(vals, {"Al-Buhaira"})


# ==========================================================================
class TestExactLookup(unittest.TestCase):
    """Case 1 + case 8: exact lookup, and exact-before-normalized priority."""

    def test_1_exact_lookup_vocalized_word(self):
        # use the spelling exactly as the CSV stores it
        target = next(s for s in csv_spellings(KEY_ZAHA) if nfc(s) == nfc(ZAHABA_FULL))
        r = lookup_exact(target)
        show("case 1: exact lookup, fully vocalized word", r)
        self.assertTrue(r.found, "expected an exact hit for the vocalized form")
        self.assertEqual(r.match_type, "exact")
        self.assertTrue(all(e.arabic == target for e in r.entries),
                        "exact mode must match the stored spelling byte for byte")
        glosses = {e.english for e in r.entries}
        self.assertIn("To go", glosses)
        self.assertGreaterEqual(r.candidate_count, 2,
                                "every sense of the exact form must be returned")

    def test_8_exact_wins_over_normalized_fallback(self):
        """ذَهَبَ ("to go") and ذَهَب ("gold") share the normalized key ذهب.

        Asking for the fully vocalized verb must NOT drag in the noun.
        """
        target = next(s for s in csv_spellings(KEY_ZAHA) if nfc(s) == nfc(ZAHABA_FULL))
        exact = lookup(target, KEY_ZAHA)
        fallback = lookup_normalized(KEY_ZAHA)
        show("case 8: exact tried first", exact)
        show("case 8: normalized fallback only", fallback)

        self.assertEqual(exact.match_type, "exact")
        self.assertTrue(any(nfc(a) == nfc(ZAHA_NOM) for a in fallback.distinct_spellings),
                        "the noun should be reachable through the fallback")
        self.assertFalse(any(nfc(a) == nfc(ZAHA_NOM) for a in exact.distinct_spellings),
                         "exact lookup leaked a different spelling")
        self.assertLess(exact.candidate_count, fallback.candidate_count)

    def test_8b_unvocalized_input_falls_through_to_normalized(self):
        r = lookup(KEY_ZAHA, KEY_ZAHA)
        show("unvocalized input -> fallback", r)
        self.assertEqual(r.match_type, "normalized")
        self.assertGreater(r.candidate_count, 1,
                           "the fallback must expose both ذَهَب and ذَهَبَ")

    def test_exact_phrase_lookup(self):
        r = lookup_exact(PHRASE_ARABIC)
        show("case 6a: exact phrase lookup", r)
        self.assertTrue(r.found)
        self.assertEqual(r.candidate_count, 1)
        self.assertEqual(r.entries[0].is_phrase, 1)

    def test_lookup_without_normalized_key_reports_instead_of_guessing(self):
        r = lookup(ABSENT_WORDS[0], None)
        self.assertFalse(r.found)
        self.assertIn("no normalized key", r.note)

    def test_lookup_accepts_injected_normalizer(self):
        r = lookup(ABSENT_WORDS[0], normalizer=lambda s: s)
        self.assertFalse(r.found)
        self.assertEqual(r.match_type, "normalized")


# ==========================================================================
class TestCollisionLookups(unittest.TestCase):
    """Cases 2-5: known collisions must return every candidate."""

    def assert_collision(self, key, expected, label):
        r = lookup_normalized(key)
        show(label, r)
        self.assertTrue(r.found, f"{key}: expected candidates")
        self.assertGreater(
            r.candidate_count, 1,
            f"{key}: collapsed to a single candidate -- a meaning was lost")
        self.assertGreater(
            len(r.distinct_spellings), 1,
            f"{key}: several rows returned but only one spelling")

        # nothing dropped: exactly the CSV's rows for this key
        self.assertEqual(
            r.candidate_count, len(csv_rows_for(key)),
            f"{key}: lookup returned {r.candidate_count} of {len(csv_rows_for(key))} rows")

        for spelling, glosses in expected:
            hits = [e for e in r.entries if nfc(e.arabic) == nfc(spelling)]
            self.assertTrue(hits, f"{key}: expected candidate spelling '{spelling}'")
            self.assertTrue(
                {h.english for h in hits} & glosses,
                f"{key}: '{spelling}' returned without any expected gloss {glosses}")

    def test_2_zahaba_verb_versus_gold(self):
        self.assert_collision(
            KEY_ZAHA,
            [(ZAHABA_FULL, {"To go", "Went", "Went away"}), (ZAHA_NOM, {"Gold"})],
            "case 2: ذهب collision (verb vs noun)")

    def test_3_amal_verb_versus_noun(self):
        self.assert_collision(
            KEY_AMAL,
            [(AMAL_MAJI, {"To work", "Did/Worked"}), (AMAL_NOUN, {"Work/Action"})],
            "case 3: عمل collision (عَمِلَ vs عَمَل)")

    def test_4_man_versus_min(self):
        self.assert_collision(
            KEY_MAN, [(MAN, {"Who?"}), (MIN, {"From / Of", "From/Of"})],
            "case 4: من collision (مَنْ vs مِنْ)")

    def test_5_kutiba_form_i_versus_kattaba_form_ii(self):
        self.assert_collision(
            KEY_KATABA,
            [(KATABA, {"To write", "Wrote"}), (KATTABA, {"To write (Made to)"})],
            "case 5: كتب collision (Form I vs Form II)")

    def test_2b_each_meaning_is_individually_reachable(self):
        by_form = {}
        for e in lookup_normalized(KEY_ZAHA).entries:
            by_form.setdefault(nfc(e.arabic), []).append(e)
        gold = by_form.get(nfc(ZAHA_NOM), [])
        self.assertEqual([e.english for e in gold], ["Gold"])
        self.assertIn("To go", [e.english for e in by_form.get(nfc(ZAHABA_FULL), [])])

    def test_4b_man_and_min_keep_their_own_glosses(self):
        glosses = {}
        for e in lookup_normalized(KEY_MAN).entries:
            glosses.setdefault(nfc(e.arabic), set()).add(e.english)
        self.assertEqual(glosses.get(nfc(MAN)), {"Who?"})
        self.assertTrue(glosses.get(nfc(MIN)) & {"From / Of", "From/Of"})
        self.assertNotIn("Who?", glosses.get(nfc(MIN), set()),
                         "مِنْ must not inherit مَنْ's gloss")

    def test_all_reviewed_collisions_return_multiple_candidates(self):
        """Bulk guard: no reviewed collision may collapse to a single row."""
        keys = _reviewed_keys()
        self.assertEqual(len(keys), EXPECTED_SPELLING_COLLISION_GROUPS)
        offenders = []
        for key in keys:
            r = lookup_normalized(key)
            if r.candidate_count <= 1 or len(r.distinct_spellings) <= 1:
                offenders.append((key, r.candidate_count, r.distinct_spellings))
        self.assertEqual(
            offenders, [],
            "these normalized keys collapsed to a single candidate:\n"
            + "\n".join(f"    {k} -> {c} row(s) {s}" for k, c, s in offenders))

    def test_all_multi_row_groups_return_every_row(self):
        """Beyond the reviewed set: every multi-row group must come back whole."""
        conn = connect()
        keys = [r["arabic_normalized"] for r in conn.execute(
            "SELECT arabic_normalized FROM entries "
            "GROUP BY arabic_normalized HAVING COUNT(*) > 1")]
        self.assertGreater(len(keys), EXPECTED_SPELLING_COLLISION_GROUPS)
        counts = {r["arabic_normalized"]: r["c"] for r in conn.execute(
            "SELECT arabic_normalized, COUNT(*) c FROM entries "
            "GROUP BY arabic_normalized")}
        bad = [(k, lookup_normalized(k).candidate_count, counts[k])
               for k in keys if lookup_normalized(k).candidate_count != counts[k]]
        self.assertEqual(bad, [], f"groups returned too few candidates: {bad[:10]}")

    def test_ambiguous_flag_is_set_for_collisions(self):
        self.assertTrue(lookup_normalized(KEY_ZAHA).is_ambiguous)
        self.assertIn("NOT unique", lookup_normalized(KEY_ZAHA).note)
        self.assertFalse(lookup_exact(PHRASE_ARABIC).is_ambiguous)


# ==========================================================================
class TestPhrasesAndMisses(unittest.TestCase):
    """Cases 6 + 7."""

    def test_6_phrase_is_kept_whole(self):
        norm = lookup_normalized(PHRASE_KEY)
        show("case 6b: phrase via normalized fallback", norm)
        self.assertTrue(norm.found)
        for e in norm.entries:
            self.assertEqual(e.arabic, PHRASE_ARABIC,
                             "phrase spelling and spacing must be preserved verbatim")
            self.assertEqual(e.is_phrase, 1)
            self.assertGreaterEqual(len(e.arabic.split()), 2)

    def test_6b_single_word_is_not_flagged_as_phrase(self):
        target = next(s for s in csv_spellings(KEY_ZAHA) if nfc(s) == nfc(ZAHA_NOM))
        r = lookup_exact(target)
        self.assertTrue(r.found)
        self.assertEqual(r.entries[0].is_phrase, 0)

    def test_6c_phrase_variants_are_not_split(self):
        for key in ("بذر البذور", "برقت السماء"):
            r = lookup_normalized(key)
            self.assertTrue(r.found, f"{key}: phrase variant group missing")
            for e in r.entries:
                self.assertGreaterEqual(len(e.arabic.split()), 2,
                                        "phrase was split into single words")

    def test_7_unknown_word_returns_nothing(self):
        for word in ABSENT_WORDS:
            r = lookup(word, word)
            show(f"case 7: absent word {word}", r)
            self.assertFalse(r.found, f"{word} should not be in the dictionary")
            self.assertEqual(r.candidate_count, 0)
            self.assertEqual(r.entries, ())
            self.assertFalse(lookup_exact(word).found)

    def test_result_helpers(self):
        r = lookup_normalized(KEY_ZAHA)
        self.assertTrue(r)
        self.assertTrue(r.is_ambiguous)
        self.assertEqual(len(r), r.candidate_count)
        self.assertEqual(list(r), list(r.entries))
        self.assertEqual(r.forms, r.distinct_spellings)
        self.assertIsNotNone(r.first())
        self.assertIsNone(lookup_normalized(ABSENT_WORDS[0]).first())
        rows = r.as_dicts()
        self.assertEqual(len(rows), r.candidate_count)
        self.assertEqual(set(rows[0]), {"id", "arabic", "arabic_normalized", "english",
                                        "malayalam", "is_phrase", "source"})
        self.assertTrue(all(isinstance(e, Entry) for e in r.entries))
        self.assertEqual(len({e.id for e in r.entries}), r.candidate_count,
                         "candidate ids must be distinct")


# ==========================================================================
class TestUnicodeBehaviour(unittest.TestCase):
    """Documents the deliberate 'store verbatim' decision about NFC."""

    def test_some_stored_forms_are_not_nfc(self):
        rows = [r for r in _csv_rows() if not unicodedata.is_normalized("NFC", r["arabic"])]
        self.assertGreater(len(rows), 0,
                           "expected some non-NFC Arabic to exist in the source")
        # and the database reproduces those byte sequences unchanged
        sample = rows[0]["arabic"]
        stored = connect().execute(
            "SELECT arabic FROM entries WHERE arabic = ?", (sample,)).fetchall()
        self.assertTrue(stored, "non-NFC spelling was altered on insert")
        self.assertEqual(stored[0][0], sample)

    def test_nfc_and_non_nfc_spellings_are_distinct_rows(self):
        """Form II كَتَّبَ is stored as U+0651 U+064E; exact matching is byte-exact.

        This is why fixtures are compared NFC-folded. It also means callers must
        NFC-fold their input before an exact lookup if the text came from an
        unknown source -- see database/README.md.
        """
        stored = csv_spellings(KEY_KATABA)
        non_nfc = [s for s in stored if not unicodedata.is_normalized("NFC", s)]
        self.assertTrue(non_nfc, "expected a non-NFC Form II spelling in the CSV")
        self.assertEqual(len(stored), 2, "Form I and Form II must both be stored")
        # a non-NFC input does NOT exact-match the NFC-composed row...
        nfc_form = unicodedata.normalize("NFC", non_nfc[0])
        self.assertNotEqual(non_nfc[0], nfc_form)
        self.assertEqual(lookup_exact(nfc_form).candidate_count, 0)
        # ...but the byte-exact row is found, and the normalized key returns all
        # rows for both forms (Form I has several senses of its own).
        self.assertEqual(lookup_exact(non_nfc[0]).candidate_count, 1)
        self.assertEqual(lookup_normalized(KEY_KATABA).candidate_count,
                         len(csv_rows_for(KEY_KATABA)))

    def test_arabic_roundtrips_without_corruption(self):
        for e in lookup_normalized(KEY_MAN).entries:
            self.assertEqual(e.arabic, e.arabic.strip().strip())
            self.assertTrue(e.arabic)
            with self.subTest(arabic=e.arabic):
                conn = sqlite3.connect(":memory:")
                conn.execute("CREATE TABLE t (a TEXT)")
                conn.execute("INSERT INTO t VALUES (?)", (e.arabic,))
                self.assertEqual(conn.execute("SELECT a FROM t").fetchone()[0], e.arabic)
                conn.close()


# ==========================================================================
class TestSwappedColumnRepair(unittest.TestCase):
    """The 340 rows extract_buh.py mis-assigned must be fixed and searchable."""

    def setUp(self):
        self.conn = connect()

    def test_no_arabic_column_value_lacks_arabic_script(self):
        bad = [r["arabic"] for r in self.conn.execute(
            "SELECT arabic FROM entries") if not _ARABIC_RE.search(r["arabic"])]
        self.assertEqual(bad, [], f"{len(bad)} row(s) have no Arabic in 'arabic'")

    def test_no_malayalam_left_in_arabic_column(self):
        bad = [r["arabic"] for r in self.conn.execute(
            "SELECT arabic FROM entries") if _ML_RE.search(r["arabic"])]
        self.assertEqual(bad, [], f"{len(bad)} row(s) still have Malayalam in 'arabic'")

    def test_every_malayalam_column_value_has_malayalam(self):
        bad = [r["malayalam"] for r in self.conn.execute(
            "SELECT malayalam FROM entries") if not _ML_RE.search(r["malayalam"])]
        self.assertEqual(bad, [], f"{len(bad)} row(s) lost their Malayalam gloss")

    def test_repaired_entries_are_found_by_exact_arabic(self):
        for arabic, norm, english, malayalam in REPAIRED_ENTRIES:
            with self.subTest(arabic=arabic):
                r = lookup_exact(arabic)
                show(f"exact repaired: {english}", r)
                self.assertTrue(r.found, f"{arabic} not found by exact lookup")
                self.assertEqual(r.match_type, "exact")
                self.assertEqual(r.entries[0].english, english)
                self.assertEqual(r.entries[0].malayalam, malayalam)
                self.assertEqual(r.entries[0].arabic_normalized, norm)

    def test_repaired_entries_are_found_by_normalized_key(self):
        for arabic, norm, english, _ in REPAIRED_ENTRIES:
            with self.subTest(key=norm):
                r = lookup_normalized(norm)
                self.assertTrue(r.found, f"{norm} not found by normalized lookup")
                glosses = {e.english for e in r.entries}
                self.assertIn(english, glosses)

    def test_repaired_entries_were_formerly_unsearchable(self):
        """Regression proof: the old broken Malayalam strings must be gone."""
        conn = connect()
        for _a, _n, _e, old_malayalam in REPAIRED_ENTRIES:
            with self.subTest(old=old_malayalam):
                hits = conn.execute(
                    "SELECT COUNT(*) FROM entries WHERE arabic = ?",
                    (old_malayalam,)).fetchone()[0]
                self.assertEqual(hits, 0,
                                 "the Malayalam string is still in the arabic column")

    def test_phrase_count_reflects_real_multi_word_arabic(self):
        """The repair un-flagged 25 rows that were 'phrases' only because the
        arabic column held multi-word Malayalam."""
        got = self.conn.execute(
            "SELECT COUNT(*) FROM entries WHERE is_phrase = 1").fetchone()[0]
        self.assertEqual(got, EXPECTED_PHRASE_ROWS)
        # is_phrase must track the ARABIC field only, never the Malayalam gloss
        mislabelled = self.conn.execute(
            "SELECT COUNT(*) FROM entries WHERE is_phrase = 1 "
            "AND LENGTH(arabic) - LENGTH(REPLACE(arabic, ' ', '')) = 0").fetchone()[0]
        self.assertEqual(mislabelled, 0, "single-word Arabic flagged as a phrase")


# ==========================================================================
class TestConnectionSafety(unittest.TestCase):
    def test_connection_is_read_only(self):
        with self.assertRaises(sqlite3.OperationalError):
            connect().execute("DELETE FROM entries WHERE id = 1")
        self.assertEqual(db_rows(), EXPECTED_ROWS, "row count must be unchanged")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print("=" * 68)
    print("buhaira.db test suite")
    print("=" * 68)
    try:
        result = unittest.main(verbosity=2, exit=False).result
    finally:
        close()
    print("\n" + "=" * 68)
    print(("TESTS PASSED" if result.wasSuccessful() else "TESTS FAILED")
          + f"  ({result.testsRun} tests, {len(result.failures)} failure(s), "
            f"{len(result.errors)} error(s))")
    print("=" * 68)
    raise SystemExit(0 if result.wasSuccessful() else 1)