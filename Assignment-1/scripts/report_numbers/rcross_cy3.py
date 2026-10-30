import csv,collections,statistics as S
import pathlib; A = str(pathlib.Path(__file__).resolve().parents[2])
for host in ['C4M7Y670NT']:
  d=collections.defaultdict(list)
  for r in csv.DictReader(open(f"{A}/results/{host}/counter.csv")):
    if r['ok']!='1' or r['outside_work']!='0': continue
    ex=dict(kv.split(':',1) for kv in r['extra'].split(';') if ':' in kv)
    d[(r['impl'],int(r['threads']))].append(float(ex['thr_min_s'])/float(ex['thr_max_s']))
  for i in ['cas','cas_yield','cas_spin','cas_sleep','lock_os_unfair_lock']:
    print(host,i,[(p,round(S.median(d[(i,p)]),2)) for p in [2,4,8,10,20] if (i,p) in d])
