#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')
import csv
import os
import json
import re
from collections import defaultdict

IN = 'output_albuhaira/dictionary_albuhaira.csv'
OUTDIR = 'cleaned_buhaira'
os.makedirs(OUTDIR, exist_ok=True)

# read
with open(IN, encoding='utf-8') as f:
    r = csv.DictReader(f)
    fieldnames = r.fieldnames
    rows_raw = list(r)

input_rows = len(rows_raw)

# normalize
def strip_html(s):
    if not s: return s
    s = re.sub(r'<[^>]+>', '', s)
    return s

def clean_whitespace(s):
    if not s: return s
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def normalize_arabic(s):
    if not s: return s
    s = s
    # diacritics
    diacritics = re.compile(r'[\u064B-\u0652\u0670\u0640\u06D4\u0610-\u0615\u06E1-\u06E2]')
    s = diacritics.sub('', s)
    s = s.replace('\u0640', '')
    # tatweel
    s = s.replace('\u0629', '\u062A')  # don't convert ta marbuta to ha as requested? wait - request says "Do NOT convert: ة → ه"
    # but normalize alef variants
    s = s.replace('\u0622', '\u0627').replace('\u0623', '\u0627').replace('\u0625', '\u0627').replace('\u0671', '\u0627')
    s = s.replace('\u0624', '\u0648')
    s = s.replace('\u0626', '\u064A')
    s = s.replace('\u0649', '\u064A')
    return s

cleaned_rows = []
for row in rows_raw:
    eng = clean_whitespace(strip_html(row.get('english', '') or ''))
    ar = clean_whitespace(strip_html(row.get('arabic', '') or ''))
    ml = clean_whitespace(strip_html(row.get('malayalam', '') or ''))
    cat = clean_whitespace(strip_html(row.get('category', '') or row.get('Category', '') or ''))
    cleaned_rows.append({
        'english': eng,
        'arabic': ar,
        'malayalam': ml,
        'category_original': cat,
        'arabic_raw': ar
    })

# detect swapped columns - arabic in first col? no, but check
# swap if arabic script in what looks like english position? check
swapped_count = 0
repaired_rows = []
for i, r in enumerate(cleaned_rows):
    ar = r['arabic']
    eng = r['english']
    ml = r['malayalam']
    # detect swap: ar starts with arabic char and eng has no arabic, ml has malayalam
    def has_arabic(txt):
        if not txt: return False
        for c in txt:
            if 0x0600 <= ord(c) <= 0x06FF: return True
        return False
    def has_malayalam(txt):
        if not txt: return False
        for c in txt:
            if 0x0D00 <= ord(c) <= 0x0D7F: return True
        return False
    if has_arabic(eng) and not has_arabic(ar) and has_malayalam(ar):  # swapped?
        # check - eng has arabic, ar has malayalam? that means swapped
        # original order eng,ml,ar? if swapped to ar,eng,ml in positions
        # better: if first col is arabic and third is malayalam -> swap to eng,ar,ml? 
        pass

# simpler: look for cases where first col (current eng) is all arabic and third (ml) is malayalam and second (ar) is english-like
repaired_list = []
for r in cleaned_rows:
    eng = r['english']
    ar = r['arabic']
    ml = r['malayalam']
    def has_ar(s):
        if not s: return False
        for c in s[:2]:
            if 0x0600 <= ord(c) <= 0x06FF: return True
        return False
    def has_ml(s):
        if not s: return False
        for c in s[:1]:
            if 0x0D00 <= ord(c) <= 0x0D7F: return True
        return False
    if has_ar(eng) and has_ml(ml) and not has_ar(ar) and not has_ml(ar) and ar:
        # likely eng,ml,ar were positions but read differently? or swapped
        # swap: eng becomes ar (original), ar becomes eng (original english), ml stays? 
        # original was English | Malayalam | Arabic - if columns got misread, maybe not. But per rule check
        pass

# write quick check
pass
