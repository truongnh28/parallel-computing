import csv,collections,statistics as S
import pathlib; A = str(pathlib.Path(__file__).resolve().parents[2])
d=collections.defaultdict(list)
for r in csv.DictReader(open(f"{A}/results/C4M7Y670NT/counter.csv")):
  if r['ok']!='1' or r['outside_work']!='0': continue
  ex=dict(kv.split(':',1) for kv in r['extra'].split(';') if ':' in kv)
  d[(r['impl'],int(r['threads']))].append((1e9*float(r['seconds'])/int(r['ops']),float(ex['thr_min_s'])/float(ex['thr_max_s'])))
for i in ['faa','cas','cas_seqcst','lock_std_mutex','lock_pthread_mutex','lock_os_unfair_lock']:
  print(i,[(p,round(S.median(x[0] for x in d[(i,p)]),1),round(min(x[0] for x in d[(i,p)]),1),round(max(x[0] for x in d[(i,p)]),1)) for p in [2,3,4,6,8,10,12,16,20,24]])
print('fair mutex',[(p,round(S.median(x[1] for x in d[('lock_std_mutex',p)]),2)) for p in [3,4,8,12,24]])
