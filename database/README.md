# `buhaira.db` — Al-Buhaira Arabic → Malayalam Dictionary (V1)

SQLite database holding the cleaned and normalized Al-Buhaira dictionary, plus
the build script, the lookup layer and the test suite.

The database is a **faithful mirror of the CSV**. It merges nothing, drops
nothing and re-normalizes nothing.

---

## 1. Purpose

The app reads Quranic Arabic text and wants a Malayalam gloss. The raw Arabic in
the text may be:

* fully vocalized (`ذَهَبَ`),
* partially vocalized (`ذَهَب`),
* unvocalized (`ذهب`),
* or spelled with a different hamza carrier (`إِسْتَيْقَظَ` vs `اِسْتَيْقَظَ`).

`entries` holds the dictionary so the app can look words up reliably in all four
cases, and — crucially — can tell the user *when a lookup is ambiguous* instead
of silently showing the wrong meaning.

## 2. Source file

| | |
|---|---|
| Authoritative input | `normalized_buhaira/dictionary_normalized.csv` |
| Columns | `english`, `arabic`, `arabic_normalized`, `malayalam` |
| Rows | 4919 |
| Produced by | `normalized_buhaira/normalize_arabic.py` (not re-run here) |
| Collision review | `normalized_buhaira/collision_review.csv` (283 groups, read-only cross-check) |

The CSV is opened **read-only** and is never written to. `build_database.py`
asserts the CSV's size and mtime are unchanged after the build.

## 3. Schema

```sql
CREATE TABLE entries (
    id                INTEGER PRIMARY KEY,
    arabic            TEXT NOT NULL,     -- original fully-vocalized form, verbatim
    arabic_normalized TEXT NOT NULL,     -- lookup key, NOT unique
    english           TEXT,              -- gloss
    malayalam         TEXT,              -- Malayalam gloss
    is_phrase         INTEGER NOT NULL DEFAULT 0,
    source            TEXT NOT NULL DEFAULT 'Al-Buhaira'
);
```

Indexes:

```sql
CREATE INDEX idx_entries_arabic            ON entries(arabic);            -- exact lookup
CREATE INDEX idx_entries_arabic_normalized ON entries(arabic_normalized); -- fallback
CREATE INDEX idx_entries_is_phrase         ON entries(is_phrase);         -- phrase filter
```

Deliberate design decisions:

* **No `UNIQUE` on `arabic` or `arabic_normalized`.** Both are indexable but
  non-unique. A `UNIQUE` index here would silently discard dictionary meanings.
* **`arabic` is stored byte-for-byte as supplied.** It is *not* normalized again
  and *not* re-encoded (no NFC/NFKC pass). The original fully-vocalized Arabic
  **is the dictionary entry identity**.
* **`arabic_normalized` is copied verbatim from the CSV.** The normalization
  algorithm is not re-implemented or modified in this database layer.
* **One CSV row = one database row.** No dedupe, no merge, no dropping. 4919 in,
  4919 out.

### Verified contents

| Metric | Value |
|---|---|
| Total rows | 4919 |
| Distinct `arabic` | 4167 |
| Distinct `arabic_normalized` | 3812 |
| Rows with `is_phrase = 1` | 1313 (1307 distinct forms) |
| Normalized keys shared by >1 row | 691 (1798 rows involved) |
| …of which fed by >1 **distinct spelling** | 286 ← the reviewed collisions |
| `PRAGMA integrity_check` | `ok` |

> **Counts changed on 2026-10-05** by the swapped-column repair
> (`normalized_buhaira/repair_swapped_columns.py`). Distinct `arabic`
> 4164 → **4167** and phrases 1338 → **1313**; see §12 for the arithmetic.
> Distinct normalized keys stayed at 3812.

## 4. Exact lookup (primary)

```sql
SELECT id, arabic, english, malayalam, is_phrase
FROM entries
WHERE arabic = ?;
```

Returns every entry whose `arabic` matches the input byte for byte. More than one
row is normal and correct: `ذَهَبَ` has three senses (*to go*, *went*, *went
away*), and **all three are returned**.

Because this is the strongest signal available, an exact hit is final. The
normalized fallback is not consulted, which is exactly what keeps `ذَهَب`
(*gold*) out of a lookup for `ذَهَبَ` (*to go*).

### Unicode caveat — exact matching is byte-exact

473 stored `arabic` values are **not NFC-normalized**: the same word appears with
different Unicode orderings of the combining marks. Concretely, Form II
`كَتَّبَ` is stored as `U+0651 U+064E` (shadda then fatha), while most keyboards
produce the composed `U+064C`. These strings look identical but compare unequal,
so an exact lookup with the composed form returns **0 rows** for that entry.

Practical guidance for the app layer:

* Segment output that came from this dictionary → exact matching is fine.
* For input from an unknown source, apply `unicodedata.normalize("NFC", s)` (or
  the platform equivalent) *before* the exact query, and rely on the normalized
  fallback to catch the rest. Never NFC-normalize the **stored** column — that
  would merge the two entries.
* This is a deliberate V1 trade-off: storing verbatim is required, and it is what
  preserves the SAFE_ORTHOGRAPHIC collision pairs for review.

## 5. Normalized fallback (secondary)

```sql
SELECT id, arabic, english, malayalam, is_phrase
FROM entries
WHERE arabic_normalized = ?;
```

Run **only** when the exact lookup returned nothing. Returns **all** candidates.

> **Never** `... WHERE arabic_normalized = ? LIMIT 1`.
> **Never** pick the first row, the lowest `id`, or "the most frequent gloss".
> Meaning choice belongs to the application layer, which has the surrounding
> sentence.

`lookup.py` contains no `LIMIT` clause at all.

## 6. Why `arabic_normalized` is NOT unique

Normalization strips harakat and folds hamza carriers and alef variants, so it
maps distinct words onto shared keys. `ذهب` alone:

| `arabic_normalized` | `arabic` | `english` | `malayalam` |
|---|---|---|---|
| ذهب | ذَهَبَ | To go | പോവുക |
| ذهب | ذَهَب | Gold | സ്വർണ്ണം |
| ذهب | ذَهَبَ | Went | പോയി |
| ذهب | ذَهَبَ | Went away | അകന്നുപോയി |

Four rows, two distinct words. A `LIMIT 1` here would show *gold* to someone
reading the verb. Same pattern for:

| Key | Colliding spellings |
|---|---|
| `عمل` | `عَمِلَ` (to work / did) vs `عَمَل` (work, noun) |
| `من` | `مَنْ` (who?) vs `مِنْ` (from) |
| `كتب` | `كَتَبَ` (Form I) vs `كَتَّبَ` (Form II) |

### Two different reasons a normalized key has several rows

This distinction explains why the raw grouping is **691** while the reviewed
collision file lists **283**:

| Count | Meaning |
|---|---|
| **286** | One normalized key fed by **more than one Arabic spelling**. These are true *normalization* collisions — exactly what `collision_review.csv` classifies (225 `LEXICALLY_DISTINCT`, 36 `SAFE_ORTHOGRAPHIC`, 23 `VOCALIZATION_VARIANT`, 2 `PHRASE_VARIANT`). |
| **691** | Normalized keys shared by **more than one row**, which also includes the **405** keys where a *single* spelling carries several senses (e.g. `ذَهَبَ` × 3). Those are multi-sense entries, not normalization collisions. |

The build validates both numbers, and cross-checks the 286 key set against
`collision_review.csv` for exact equality.

## 7. Collision handling

1. Every CSV row is inserted. No group is collapsed, merged or deduplicated.
2. No `UNIQUE` constraint or unique index exists on either Arabic column.
3. Validation compares every multi-row group's row count against the CSV, so a
   lost row fails the build.
4. The test suite sweeps **all 286** reviewed collisions plus **all 691**
   multi-row groups and fails if any returns fewer rows than the CSV holds.
5. **Disambiguation is deferred to the app layer.** `lookup()` exposes
   `entries`, `candidate_count`, `is_ambiguous` and `distinct_spellings` so the
   UI can offer a choice or rank candidates by context.

## 8. Lookup API

```python
from database.lookup import lookup, lookup_exact, lookup_normalized, close

r = lookup("ذَهَبَ")                # 1. exact first
r = lookup("ذهب", "ذهب")           # 2. fallback, all candidates
r = lookup("ملف", "ملف")            # 3. nothing -> r.found is False

r.match_type        # 'exact' | 'normalized'
r.found             # bool
r.candidate_count   # int, > 1 means the caller must disambiguate
r.is_ambiguous      # bool
r.distinct_spellings# ('ذَهَبَ', 'ذَهَب') -- the actual ambiguity
r.forms             # alias of the above
r.entries           # tuple[Entry, ...]  -- every candidate
r.as_dicts()        # list[dict]          -- easy for the app layer
r.first()           # preview only; NOT a disambiguation decision
```

`lookup(arabic, arabic_normalized=None)` priority:

1. `lookup_exact(arabic)`.
2. If it found rows → return them. Done; the fallback is not consulted.
3. Only on a miss, `lookup_normalized(arabic_normalized)`.
4. If `arabic_normalized` is omitted and no `normalizer=` callable is passed,
   the fallback is **skipped with an explanatory `note`** rather than
   re-normalizing behind your back. The algorithm lives in
   `normalized_buhaira/normalize_arabic.py` and is not duplicated here.

The connection is opened **read-only** (`file:…?mode=ro`), so lookup code can
never modify the database. Point at a different file with
`lookup(..., db_path=...)` or the `BUHAIRA_DB` environment variable.

## 9. Phrases

`is_phrase = 1` when the `arabic` field contains more than one whitespace
separated word; otherwise `0`.

* Phrases are stored **whole**, exactly as they appear in the CSV — never split
  into individual words, and spelling/spacing are untouched
  (`طَرَقَ البَابَ`, "to knock (at the door)").
* 1313 rows are flagged.
* 281 rows use `/` as an alternatives separator inside the `arabic` field (e.g.
  `حَلِيب / لَبَن`). They are multi-word by the stated rule, so they are flagged
  as phrases and stored verbatim.

## 10. Rebuild

```bash
python database/build_database.py
```

Rebuilds from the CSV every time: any existing `buhaira.db` (plus `-wal`,
`-shm`, `-journal`) is deleted first, so runs are idempotent and never append
duplicate rows. Options:

```bash
python database/build_database.py --csv path/to.csv --db path/to/out.db
```

The script uses only Python's built-in `sqlite3` (no ORM), parameterized SQL,
a single transaction for the insert, indexes created after the load, and prints
a report. **It exits non-zero if any validation check fails.**

## 11. Run the tests

```bash
python database/test_database.py
python -m unittest discover -s database -v
```

43 tests covering row counts, schema shape, index shape, phrase flags, empty
values, integrity, the four named collision cases (`ذهب`, `عمل`, `من`, `كتب`),
phrase handling, missing words, exact-before-fallback priority, the
swapped-column repair (repaired entries findable, old Malayalam strings gone),
and a bulk sweep of every collision group. Non-zero exit on failure.

The suite fails loudly if a collision collapses: deleting all but one row of the
`ذهب`, `عمل`, `من` and `كتب` groups produces **18 failures** and exit code 1.

## 12. The 340 swapped-column rows (repaired 2026-10-05)

`Al-Buhaira Dictionary/clean_buhaira_code/extract_buh.py` parses
`dictionary home.html` assuming the table columns are
`(English, Malayalam, Arabic)` — which matches the header
`English | മലയാളം | عربي`. That holds for 4778 data rows, but **340 rows in the
HTML are rotated** to `(Arabic, English, Malayalam)`. The extractor wrote them
as `english=<Arabic>, arabic=<Malayalam>, malayalam=<English>`, leaving those
340 entries unsearchable by Arabic.

Repaired by `normalized_buhaira/repair_swapped_columns.py` as a pure 3-way
rotation of values already in the row — **no translation was invented, changed or
improved**, and the mapping was confirmed against the original HTML for
340/340 rows:

```
new_arabic    = old_english      (html cell 0)
new_english   = old_malayalam    (html cell 1)
new_malayalam = old_arabic       (html cell 2)
```

`arabic_normalized` was recomputed with the existing `normalize_arabic` (loaded
from `normalize_arabic.py` via AST — no second normalizer was written). The
other **4579 rows are byte-for-byte unchanged** in all four fields.

Backups: `dictionary_normalized_before_swapped_fix.csv` and
`Al-Buhaira Dictionary/cleaned_buhaira/dictionary_clean_before_swapped_fix.csv`.
Audit trail: `swapped_rows_repair_report.csv` / `.json`.

### Why two counts moved

| Metric | Before | After | Why |
|---|---|---|---|
| Distinct `arabic` | 4164 | **4167** | The 340 rows held only **332** distinct values in the `arabic` column but hold **335** distinct Arabic forms after repair, and none collide with the other 4579 rows: `4164 − 332 + 335 = 4167`. The 5 repeated Arabic forms are legitimate distinct senses of one word (e.g. `الثَّانِيَةُ` = "Second (time unit)" *and* "Two o'clock"), kept because the upstream cleaner deduplicated on the whole `(english, arabic, malayalam)` tuple. |
| Phrase rows | 1338 | **1313** | 293 of the swapped rows were flagged as phrases *only because* the `arabic` column held multi-word Malayalam; only 268 are genuinely multi-word Arabic. The other 25 are correctly single words — a fix, not a regression. |
| Reviewed collisions | 283 | **286** | 3 genuine new collisions (`الثغر`, `الثلاثاء`, `الثمانية`) because real Arabic now shares normalized keys with existing entries. All 283 previous keys survive unchanged; the 3 new ones classified as `LEXICALLY_DISTINCT` by the reviewer's own fallback. Worth a human glance. |

### Remaining data observations (reported, not "fixed")

`build_database.py` prints these on every run. The database mirrors the CSV
faithfully; further repairs are separate upstream decisions.

| Observation | Count | Impact |
|---|---|---|
| Swapped-column rows | **0** | Fixed. Build check `E0` fails if any ever reappear. |
| Rows with an Arabic word inside the English gloss (e.g. `الكعبة (Kaaba)`) | 6 | Pre-existing and correct — not a swapped column. |
| Rows using `/` as an alternatives separator inside `arabic` | 281 | Stored verbatim; flagged as phrases. |
| Rows not NFC-normalized | 473 | Exact lookup is byte-sensitive; see §4. Source of the 36 `SAFE_ORTHOGRAPHIC` groups. |
| Arabic forms carrying more than one English sense | 520 | All rows kept — meanings are never dropped. |

## 13. Files

```
database/
    buhaira.db           the SQLite database (built artifact)
    build_database.py    schema + import + indexes + validation + report
    lookup.py            exact lookup, normalized fallback, lookup() priority
    test_database.py     36 tests
    README.md            this file
```

`buhaira.db` is reproducible from `normalized_buhaira/dictionary_normalized.csv`
alone. Nothing in it is hand-edited, and no build step depends on manually
edited database contents.

---

**Entry identity = the original fully-vocalized Arabic.** Normalization exists
only to *find* an entry as a fallback; it never defines one, and it never merges
two.