import csv,collections,statistics as S
import pathlib; A = str(pathlib.Path(__file__).resolve().parents[2])
d=collections.defaultdict(list)
for r in csv.DictReader(open(f"{A}/results/LAP1184/counter.csv")):
  if r['ok']!='1' or r['outside_work']!='0': continue
  d[(r['impl'],int(r['threads']))].append(1e9*float(r['seconds'])/int(r['ops']))
for p in [6,8,10,12]: print('mcs',p,sorted(round(x) for x in d[('lock_mcs',p)]))
m=lambda i,p: S.median(d[(i,p)])
for i in ['lock_std_mutex','lock_pthread_mutex','lock_omp_lock','lock_tas','lock_pthread_spinlock']:
  print(i,[(p,round(m(i,p)/m('faa',p),2)) for p in [2,3,4,6,8,10,12,16,20,24]])
