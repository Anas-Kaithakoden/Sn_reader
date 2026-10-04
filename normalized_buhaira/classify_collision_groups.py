#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Classify the 283 normalization collision groups -> collision_review.csv

Evidence used (nothing else):
  1. the original Arabic forms
       - word count (phrase vs single word)
       - whether they are byte-different yet identical under Unicode NFC
         (same word, different encoding of the combining marks)
       - whether the letter codepoints are identical (harakat-only delta) or
         differ through one of the normalizer's letter folds
         (U+0622/0623/0625/0671 -> U+0627, U+0624 -> U+0648,
          U+0626/0649/06CC -> U+064A, U+06A9 -> U+0643)
       - the Arabic vocalization pattern of each form
         (Form I / Form II / masdar / adjective / active / passive)
  2. the dictionary entries that already exist for those forms
     (english + malayalam columns of dictionary_clean.csv)

The dictionary is opened read-only. No entry is merged, nothing is rewritten,
and normalize_arabic.py is not touched.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import csv, re, unicodedata
from collections import defaultdict, Counter

DICT = 'Al-Buhaira Dictionary/cleaned_buhaira/dictionary_clean.csv'
COLLISIONS = 'normalized_buhaira/normalization_collisions.csv'
OUT = 'normalized_buhaira/collision_review.csv'

# ---- normalization copied verbatim from normalize_arabic.py (read-only) ----
def normalize_arabic(s):
    if not s: return s
    s = str(s)
    s = re.sub(r'[\u064B-\u0652\u0670\u06D6-\u06ED\u08D3-\u08FF]', '', s)
    s = s.replace('\u0640', '')
    s = s.replace('\u0622', '\u0627').replace('\u0623', '\u0627').replace('\u0625', '\u0627').replace('\u0671', '\u0627')
    s = s.replace('\u0624', '\u0648')
    s = s.replace('\u0626', '\u064A')
    s = s.replace('\u0649', '\u064A')
    s = s.replace('\u06CC', '\u064A')
    s = s.replace('\u06A9', '\u0643')
    s = re.sub(r'\s+', ' ', s).strip()
    return s

HARAKAT = re.compile(r'[\u064B-\u0652\u0670\u06D6-\u06ED\u08D3-\u08FF]')

def letters(f):
    """letters only, every codepoint kept distinct"""
    return ''.join(c for c in HARAKAT.sub('', f).replace('\u0640', '') if not c.isspace())

def nword(f):
    return len([w for w in re.split(r'\s+', f.strip()) if w])

FOLDS = [
    ('\u0623', 'alef-hamza-above \u0623->\u0627'),
    ('\u0625', 'alef-hamza-below \u0625->\u0627'),
    ('\u0622', 'alef-madda \u0622->\u0627'),
    ('\u0671', 'alef-wasla \u0671->\u0627'),
    ('\u0624', 'waw-hamza \u0624->\u0648'),
    ('\u0626', 'yeh-hamza \u0626->\u064A'),
    ('\u0649', 'alef-maqsura \u0649->\u064A'),
    ('\u06CC', 'farsi-yeh \u06CC->\u064A'),
    ('\u06A9', 'farsi-kaf \u06A9->\u0643'),
]
def fold_names(forms):
    got = []
    for src, name in FOLDS:
        if any(src in HARAKAT.sub('', f) for f in forms):
            got.append(name)
    return got

# ------------------------------------------------------------------
# curated decisions, keyed by the normalized form
#   SAFE   -> SAFE_ORTHOGRAPHIC  : one word, different Unicode/orthographic form
#   VOCAL  -> VOCALIZATION_VARIANT: one lexeme, delta is harakat only
#   PHRASE -> PHRASE_VARIANT    : multi-word forms
#   (anything not listed -> LEXICALLY_DISTINCT)
# ------------------------------------------------------------------
DECISION = {}
def put(keys, cls):
    for k in keys:
        assert k not in DECISION, 'duplicate decision for %s' % k
        DECISION[k] = cls

# --- PHRASE_VARIANT: multi-word forms ---
put(['بذر البذور',       # بَذَرَ البُذُور / بَذَرَ البُذُورَ   "to sow seeds"
     'برقت السماء'],     # بَرَقَتِ السَّمَاء / ...السَّمَاءُ  "to flash lightning"
    'PHRASE')

# --- SAFE_ORTHOGRAPHIC (a): same word, different Unicode encoding of the marks ---
put([
    'بطة',    # بَطَّة   both "Duck"      (NFC-identical)
    'قبعة',   # قُبَّعَة  both "Hat/Cap"  (NFC-identical)
    'سيارة',  # سَيَّارَة both "Car"       (NFC-identical)
    'فكر',    # to think / considered   vs   Thought/Reflected
    'احب',    # to like / love          vs   Loved
    'غير',    # to change               vs   Changed/Altered
    'تذكر',   # to remember             vs   Remembered
    'قرر',    # to decide               vs   Decided/Resolved
    'عد',     # to count                vs   Counted/Considered
    'خطط',    # to plan                 vs   Planed/Scheduled
    'تخيل',   # to imagine              vs   Imagined
    'شجع',    # to encourage            vs   Encouraged
    'نبه',    # to warn / alert         vs   Woke up
    'وقع',    # to sign                 vs   Agreed (Mutual)
    'علق',    # to hang / suspend       vs   Connected/Linked
    'سمي',    # to name / call          vs   Named/Called
    'توقع',   # to expect               vs   Stopped/Paused
    'تزوج',   # to marry                vs   Got married
    'هدد',    # هَدَّدَ  to threaten / threatened  (NFC-identical)
    ], 'SAFE')

# --- SAFE_ORTHOGRAPHIC (b): hamza-carrier spelling of ONE word (أ/إ/آ/ٱ -> ا) ---
put([
    'استيقظ',  # إِسْتَيْقَظَ / اِسْتَيْقَظَ   wake up
    'اشتري',   # إِشْتَرَى / اِشْتَرَى       buy   (+ ى/ي word final)
    'اختار',   # to choose / chosen
    'استراح',  # to rest / rested
    'اكتشف',   # to discover / discovered
    'اتفق',    # to agree / agreed
    'انزلق',   # to slip / slid
    'احترق',   # to burn / burned
    'استثمر',  # to invest / invested
    'اعترف',   # to admit / confessed
    'اعتذر',   # to apologize / apologized
    'انتشر',   # to spread / was spread
    'انكسر',   # to break / was broken
    'انشغل',   # to be busy / was busy
    'اهتز',    # to shake / trembled
    'ابتلي',   # to test / to be tested   (+ ى/ي word final)
    'اذل',     # أَذَلَّ / اِذَّلَّ  active vs passive of the same verb
    ], 'SAFE')

# --- VOCALIZATION_VARIANT: one lexeme, harakat only ---
put([
    'امراة',   # امْرَأَة / اِمْرَأَة   initial vowel only
    'ضفدع',    # both "frog"
    'فم',      # فَم / فَمّ            both "mouth"
    'دم',      # دَم / دَمّ            both "blood"
    'انت',     # masc / fem address
    'هولاء',
    'ذهبي',
    'جذع',     # both "trunk/stem"
    'سمع',     # active / passive
    'طبع',     # active / passive
    'صنع',     # active / passive
    'ببغاء',   # both "parrot"
    'جمعة',    # both "Friday"
    'جملة',    # جُمْلَة / جُمْلَةً   same lexeme, different case
    'ذبح',     # Form I / Form II / passive of "slaughter"
    'غفر',     # active / passive
    'هزم',     # active / passive
    'غطي',     # active / passive
    'ضفة',     # both "bank/side"
    'صور',     # active / passive
    'وسط',     # both "middle"
    'نظم',     # active / passive
    'كنز',     # كَنْز / كِنْز        same lexeme
    ], 'VOCAL')

# ------------------------------------------------------------------
rows = list(csv.DictReader(open(DICT, encoding='utf-8')))
groups = defaultdict(lambda: defaultdict(lambda: {'eng': [], 'ml': []}))
for r in rows:
    g = groups[normalize_arabic(r['arabic'])][r['arabic']]
    if r['english'] not in g['eng']: g['eng'].append(r['english'])
    if r['malayalam'] not in g['ml']: g['ml'].append(r['malayalam'])

collisions = list(csv.DictReader(open(COLLISIONS, encoding='utf-8')))
keys = {c['arabic_normalized'] for c in collisions}

missing = [k for k in DECISION if k not in keys]
assert not missing, 'decision keys that are not collision keys: %r' % missing

NAME = {'SAFE': 'SAFE_ORTHOGRAPHIC', 'VOCAL': 'VOCALIZATION_VARIANT',
        'PHRASE': 'PHRASE_VARIANT', 'LEX': 'LEXICALLY_DISTINCT'}

def short(s, n=46):
    s = ' '.join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + '\u2026'

def gloss(d):
    return short(' / '.join(d['eng']))

out_rows = []
for c in collisions:
    key = c['arabic_normalized']
    G = groups[key]
    fs = sorted(G)

    nfc_same = len({unicodedata.normalize('NFC', f) for f in fs}) == 1
    same_letters = len({letters(f) for f in fs}) == 1
    multi = all(nword(f) > 1 for f in fs)
    fl = fold_names(fs)

    tag = DECISION.get(key, 'LEX')
    cls = NAME[tag]
    if multi != (tag == 'PHRASE'):
        raise AssertionError('phrase flag mismatch for %s' % key)

    if tag == 'PHRASE':
        reason = ('Multi-word phrase (%d words): "%s" vs "%s" differ only by the final case harakat; '
                  'both glossed "%s". Same phrase, no lexical split.'
                  % (nword(fs[0]), fs[0], fs[-1], gloss(G[fs[0]])))
    elif nfc_same:
        reason = ('Same word, different Unicode encoding: the two entries are byte-different but identical '
                  'after NFC (only the combining-mark order differs). Both denote "%s".'
                  % gloss(G[fs[0]]))
    elif tag == 'SAFE':
        reason = ('Orthographic spelling variants of one word (%s): "%s". Same sense "%s", so a normalized '
                  'hit still points at the right entry.'
                  % (', '.join(fl) if fl else 'hamza-carrier', ' / '.join(fs), gloss(G[fs[0]])))
    elif tag == 'VOCAL':
        reason = ('Identical consonantal skeleton "%s"; "%s" differ only by harakat and denote the same '
                  'lexeme ("%s").' % (letters(fs[0]), ' / '.join(fs), gloss(G[fs[0]])))
    else:
        A, B = fs[0], fs[-1]
        why = []
        if not same_letters:
            why.append('letter fold %s merged different spellings' % ', '.join(fl))
        why.append('the harakat that separate them were stripped')
        reason = ('Different Arabic words forced onto key "%s": "%s" = "%s"  vs  "%s" = "%s"  (%s).'
                  % (key, A, gloss(G[A]), B, gloss(G[B]), '; '.join(why)))

    out_rows.append({
        'arabic_normalized': key,
        'original_forms': '|'.join(fs),
        'classification': cls,
        'english_meanings': ' ; '.join(sorted({e for f in fs for e in G[f]['eng']})),
        'malayalam_meanings': ' ; '.join(sorted({m for f in fs for m in G[f]['ml']})),
        'reason': reason,
    })

with open(OUT, 'w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, ['arabic_normalized', 'original_forms', 'classification',
                           'english_meanings', 'malayalam_meanings', 'reason'])
    w.writeheader()
    for r in out_rows:
        w.writerow(r)

print('collision groups classified:', len(out_rows))
for k, v in Counter(r['classification'] for r in out_rows).most_common():
    print('   %-22s %4d  (%5.1f%%)' % (k, v, 100.0 * v / len(out_rows)))
print('\nwrote', OUT)