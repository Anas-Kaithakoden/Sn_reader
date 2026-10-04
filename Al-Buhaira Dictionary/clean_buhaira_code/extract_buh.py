import sys
sys.stdout.reconfigure(encoding='utf-8')
import re
import csv
import os

with open('Al-Buhaira/dictionary home.html', encoding='utf-8', errors='replace') as f:
    html = f.read()

rows = []
for m in re.finditer(r'<tr[^>]*>.*?</tr>', html, re.DOTALL):
    tr = m.group(0)
    tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.DOTALL)
    if len(tds) >= 3:
        eng = re.sub(r'<[^>]+>', '', tds[0]).strip()
        ml = re.sub(r'<[^>]+>', '', tds[1]).strip()
        ar = re.sub(r'<[^>]+>', '', tds[2]).strip()
        if eng.lower() == 'english': continue
        if ar:
            rows.append((eng, ar, ml))
os.makedirs('output_albuhaira', exist_ok=True)
with open('output_albuhaira/dictionary_albuhaira.csv', 'w', newline='', encoding='utf-8') as f:
    w = csv.writer(f)
    w.writerow(['english','arabic','malayalam'])
    for r in rows: w.writerow(r)
print(len(rows))
