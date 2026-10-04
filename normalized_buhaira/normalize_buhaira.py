#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')
import csv
import os
import json
import re

IN = 'cleaned_buhaira/dictionary_clean.csv'
OUTDIR = 'normalized_buhaira'
os.makedirs(OUTDIR, exist_ok=True)

def normalize_arabic(s):
    if not s: return s
    s = str(s)
    # diacritics
    s = re.sub(r'[\u064B-\u0652\u0670\u06D6-\u06ED\u08D3-\u08FF]', '', s)
    # tatweel
    s = s.replace('\u0640', '')
    # alef variants
    s = s.replace('\u0622','\u0627').replace('\u0623','\u0627').replace('\u0625','\u0627').replace('\u0671','\u0627')
    # hamza carriers for lookup
    s = s.replace('\u0624','\u0648')  # waw with hamza above -> waw
    s = s.replace('\u0626','\u064A')  # ya with hamza above -> ya
    # ya variants
    s = s.replace('\u0649','\u064A')
    s = s.replace('\u06CC','\u064A')  # persian yeh
    # kaf persian
    s = s.replace('\u06A9','\u0643')
    # normalize whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s

with open(IN, encoding='utf-8') as f:
    r = csv.DictReader(f)
    rows = list(r)

# write normalized
out_rows = []
for row in rows:
    ar = row['arabic']
    ar_n = normalize_arabic(ar)
    out_rows.append({
        'english': row['english'],
        'arabic': ar,
        'arabic_normalized': ar_n,
        'malayalam': row['malayalam']
    })

with open(os.path.join(OUTDIR, 'dictionary_normalized.csv'), 'w', newline='', encoding='utf-8') as f:
    w = csv.writer(f)
    w.writerow(['english','arabic','arabic_normalized','malayalam'])
    for orow in out_rows:
        w.writerow([orow['english'], orow['arabic'], orow['arabic_normalized'], orow['malayalam']])

# tests
tests = ['أَب','أَبٌ','أَبُ','أَبِ','أَبْ','آدم','أدم','إدم','ٱدم','كتاب','كــــتاب','الكتاب','والكتاب','ى','ي','ک','ك','مؤمن','مومن','مسؤول','مسئول','ة','كتابة','كتابه','ذِكْر','ذَكَرَ','ذُكِرَ','في كل مكان','فِي كُلِّ مَكَان']
with open(os.path.join(OUTDIR, 'normalization_tests.csv'), 'w', newline='', encoding='utf-8') as f:
    w = csv.writer(f)
    w.writerow(['input','normalized','notes'])
    for t in tests:
        w.writerow([t, normalize_arabic(t), ''])

# collisions
from collections import defaultdict
groups = defaultdict(lambda: {'orig': set(), 'eng': set(), 'ml': set()})
for orow in out_rows:
    groups[orow['arabic_normalized']]['orig'].add(orow['arabic'])
    groups[orow['arabic_normalized']]['eng'].add(orow['english'])
    groups[orow['arabic_normalized']]['ml'].add(orow['malayalam'])

collisions = []
for key, data in groups.items():
    if len(data['orig']) > 1:
        collisions.append({
            'arabic_normalized': key,
            'original_forms': sorted(list(data['orig'])),
            'row_count': len(data['orig']),  # approx
            'english_meanings': len(data['eng']),
            'malayalam_meanings': len(data['ml']),
            'collision_type': 'UNKNOWN'
        })

with open(os.path.join(OUTDIR, 'normalization_collisions.csv'), 'w', newline='', encoding='utf-8') as f:
    w = csv.writer(f)
    w.writerow(['arabic_normalized','original_forms','row_count','english_meanings','malayalam_meanings','collision_type'])
    for c in collisions:
        w.writerow([c['arabic_normalized'], '|'.join(c['original_forms']), c['row_count'], c['english_meanings'], c['malayalam_meanings'], c['collision_type']])

# report
unique_orig = len(set(r['arabic'] for r in rows))
unique_norm = len(set(normalize_arabic(r['arabic']) for r in rows))
rows_changed = sum(1 for r in rows if normalize_arabic(r['arabic']) != r['arabic'])

report = {
  "input_rows": len(rows),
  "unique_original_arabic": unique_orig,
  "unique_normalized_arabic": unique_norm,
  "normalization_reduction": unique_orig - unique_norm,
  "rows_changed": rows_changed,
  "collision_groups": len(collisions),
  "safe_collision_groups": 0,
  "vocalization_collision_groups": 0,
  "potentially_different_word_groups": 0,
  "unknown_collision_groups": len(collisions)
}
with open(os.path.join(OUTDIR, 'normalization_report.json'), 'w', encoding='utf-8') as f:
    json.dump(report, f, indent=2, ensure_ascii=False)

readme = '''# Arabic Normalization\n\nSafe normalization preserving original Arabic. No prefixes removed. ة preserved.'''
with open(os.path.join(OUTDIR, 'README.md'), 'w', encoding='utf-8') as f:
    f.write(readme)

# copy script
import shutil
shutil.copy('normalize_buhaira.py', os.path.join(OUTDIR, 'normalize_arabic.py'))

print(report)
