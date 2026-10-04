#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')
import csv
import re
import os
import json
from collections import defaultdict

IN = 'dictionary_albuhaira.csv'
OUTDIR = 'cleaned_buhaira'
os.makedirs(OUTDIR, exist_ok=True)

# read
rows_raw = []
with open(IN, encoding='utf-8', errors='replace') as f:
    r = csv.DictReader(f)
    # detect columns - original likely has english,malayalam,arabic,category?
    for i, row in enumerate(r):
        rows_raw.append(row)

print('raw', len(rows_raw))
print(list(rows_raw[0].keys()) if rows_raw else 'empty')
