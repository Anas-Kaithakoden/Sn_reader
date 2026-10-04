#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')
import csv
import os

IN = 'output_albuhaira/dictionary_albuhaira.csv'
with open(IN, encoding='utf-8') as f:
    r = csv.DictReader(f)
    rows = list(r)
    print('rows', len(rows), 'keys', r.fieldnames)
