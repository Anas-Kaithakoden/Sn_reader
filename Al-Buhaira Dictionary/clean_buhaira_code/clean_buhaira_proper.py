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

with open(IN, encoding='utf-8') as f:
    r = csv.DictReader(f)
    rows = list(r)

def has_ar(s):
    if not s: return False
    for c in str(s)[:5]:
        if 0x0600 <= ord(c) <= 0x06FF: return True
    return False

def has_ml(s):
    if not s: return False
    for c in str(s)[:5]:
        if 0x0D00 <= ord(c) <= 0x0D7F: return True
    return False

# detect swapped: sometimes data might be ar,eng,ml? 
# but we extracted as eng,ar,ml - check
# look for rows where eng is arabic and ar is english-like
swapped = []
fixed = []
for row in rows:
    eng = str(row.get('english',''))
    ar = str(row.get('arabic',''))
    ml = str(row.get('malayalam',''))
    if has_ar(eng) and has_ml(ml) and not has_ar(ar):
        # swapped case? maybe original was ar,eng,ml - but our extraction put wrong order
        # treat as: ar_col=eng (orig), eng_col=ar (orig), ml_col=ml (orig) -> swap to eng=ar, ar=eng, ml=ml
        swapped.append((eng,ar,ml))
        fixed.append({'english': ar, 'arabic': eng, 'malayalam': ml})
    else:
        fixed.append({'english': eng, 'arabic': ar, 'malayalam': ml})

print('swapped detected:', len(swapped))
for s in swapped[:5]:
    print(s)
