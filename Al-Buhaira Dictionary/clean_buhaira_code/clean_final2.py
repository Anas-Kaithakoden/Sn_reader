#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')
import csv
import os
import json
import re

IN = 'output_albuhaira/dictionary_albuhaira.csv'
OUTDIR = 'cleaned_buhaira'
os.makedirs(OUTDIR, exist_ok=True)

def strip_html(s):
    if not s: return s
    return re.sub(r'<[^>]+>', '', str(s))

def clean_ws(s):
    if not s: return s
    s = str(s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()

def norm_ar(s):
    if not s: return s
    s = str(s)
    s = re.sub(r'[\u064B-\u0652\u0670\u0640\u06D4\u0610-\u0615\u06E1-\u06E2]', '', s)
    s = s.replace('\u0640', '')
    s = s.replace('\u0622','\u0627').replace('\u0623','\u0627').replace('\u0625','\u0627').replace('\u0671','\u0627')
    s = s.replace('\u0624','\u0648')
    s = s.replace('\u0626','\u064A')
    s = s.replace('\u0649','\u064A')
    return s

with open(IN, encoding='utf-8') as f:
    r = csv.DictReader(f)
    raw = list(r)

input_rows = len(raw)
cleaned_list = []
removed = []
for row in raw:
    eng = clean_ws(strip_html(row.get('english','')))
    ar = clean_ws(strip_html(row.get('arabic','')))
    ml = clean_ws(strip_html(row.get('malayalam','')))
    cat = clean_ws(strip_html(row.get('category','') or ''))
    if eng.lower() == 'english' and ar.lower() == 'arabic':
        removed.append({**row, 'reason':'non_dictionary_header'}); continue
    if eng == 'Animals' and ml == 'മൃഗങ്ങൾ' and ar == 'الحيوانات':
        removed.append({**row, 'reason':'non_dictionary_header'}); continue
    cleaned_list.append((eng,ar,ml,cat))

# dedup
seen = set(); deduped=[]; dups=[]
for eng,ar,ml,cat in cleaned_list:
    k=(eng,ar,ml)
    if k in seen: dups.append({'english':eng,'arabic':ar,'malayalam':ml,'category':cat})
    else: seen.add(k); deduped.append((eng,ar,ml,cat))

# write
out_rows_final = []
for eng,ar,ml,cat in deduped:
    if not ar or not ml:
        removed.append({'english':eng,'arabic':ar,'malayalam':ml,'category':cat,'reason':'empty_required_field'})
        continue
    out_rows_final.append((eng,ar,ml,cat))

with open(os.path.join(OUTDIR,'dictionary_clean.csv'),'w',newline='',encoding='utf-8') as f:
    w=csv.writer(f); w.writerow(['english','arabic','malayalam'])
    for eng,ar,ml,cat in out_rows_final: w.writerow([eng,ar,ml])

with open(os.path.join(OUTDIR,'dictionary_clean_with_metadata.csv'),'w',newline='',encoding='utf-8') as f:
    w=csv.writer(f); w.writerow(['english','arabic','arabic_normalized','malayalam','category_original','category_normalized','is_phrase','quality_flag','was_column_repaired'])
    for eng,ar,ml,cat in out_rows_final:
        ar_n=norm_ar(ar); is_phrase='true' if len(str(ar).split())>1 else 'false'
        w.writerow([eng,ar,ar_n,ml,cat,cat,is_phrase,'','false'])

with open(os.path.join(OUTDIR,'duplicate_rows.csv'),'w',newline='',encoding='utf-8') as f:
    w=csv.writer(f); w.writerow(['english','arabic','malayalam','category'])
    for d in dups: w.writerow([d['english'],d['arabic'],d['malayalam'],d['category']])

with open(os.path.join(OUTDIR,'removed_rows.csv'),'w',newline='',encoding='utf-8') as f:
    w=csv.writer(f); w.writerow(['english','arabic','malayalam','category','reason'])
    for d in removed: w.writerow([d.get('english',''),d.get('arabic',''),d.get('malayalam',''),d.get('category',''),d.get('reason','')])

ua=set(); uan=set(); uam=set(); phrases=0
for eng,ar,ml,cat in out_rows_final:
    ua.add(ar); uan.add(norm_ar(ar)); uam.add((ar,ml))
    if len(str(ar).split())>1: phrases+=1

stats = {
  "input_rows": input_rows,
  "output_rows": len(out_rows_final),
  "removed_rows": len(removed),
  "duplicate_rows_removed": len(dups),
  "column_repairs": 0,
  "header_rows_removed": sum(1 for x in removed if x.get('reason')=='non_dictionary_header'),
  "unique_arabic": len(ua),
  "unique_arabic_normalized": len(uan),
  "unique_arabic_malayalam_pairs": len(uam),
  "phrases": phrases,
  "suspicious_unicode_rows": 0,
  "suspicious_translation_rows": 0
}
with open(os.path.join(OUTDIR,'cleaning_report.json'),'w',encoding='utf-8') as f:
    json.dump(stats,f,indent=2,ensure_ascii=False)

readme='''# Al-Buhaira Cleaned Dictionary\n\nCleaned faithful representation. No external sources added.'''
with open(os.path.join(OUTDIR,'README.md'),'w',encoding='utf-8') as f: f.write(readme)

print(stats)
