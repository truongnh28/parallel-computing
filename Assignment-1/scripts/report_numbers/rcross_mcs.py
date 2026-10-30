import csv,sys,collections
import pathlib; A = str(pathlib.Path(__file__).resolve().parents[2])
for host in ["C4M7Y670NT"]:
  rows=collections.defaultdict(list)
  for r in csv.DictReader(open(f"{A}/results/{host}/lock.csv")):
    if r['impl']=='mcs' and r['n']=='1009' and r['outside_work']=='0':
      rows[int(r['threads'])].append((r['ok'],int(r['iters']),float(r['seconds']),r['extra']))
  for p in sorted(rows):
    print(host,p,[(o,i,round(s,4)) for o,i,s,e in rows[p]])
