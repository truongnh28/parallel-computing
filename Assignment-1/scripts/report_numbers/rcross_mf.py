import csv,collections,statistics as S
import pathlib; A = str(pathlib.Path(__file__).resolve().parents[2])
for host in ['LAP1184','apple-m5']:
  d=collections.defaultdict(list)
  for r in csv.DictReader(open(f"{A}/results/{host}/counter.csv")):
    if r['ok']!='1' or r['outside_work']!='0': continue
    ex=dict(kv.split(':',1) for kv in r['extra'].split(';') if ':' in kv)
    d[(r['impl'],int(r['threads']))].append(float(ex['thr_min_s'])/float(ex['thr_max_s']))
  v=[S.median(d[(i,p)]) for i in ['lock_std_mutex','lock_pthread_mutex'] for p in [8,10,12,16,20,24] if (i,p) in d]
  print(host,round(min(v),3),round(max(v),3))
