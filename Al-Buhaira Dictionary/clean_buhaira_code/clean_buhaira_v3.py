#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')
import csv
import os
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
    for c in str(s):
        if 0x0600 <= ord(c) <= 0x06FF: return True
    return False

def has_ml(s):
    if not s: return False
    for c in str(s):
        if 0x0D00 <= ord(c) <= 0x0D7F: return True
    return False

# look closer
sw = 0
for row in rows[:100]:
    eng = str(row['english']); ar = str(row['arabic']); ml = str(row['malayalam'])
    print(repr(eng), repr(ar), repr(ml))
    break
