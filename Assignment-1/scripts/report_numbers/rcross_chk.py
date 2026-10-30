import csv,collections,statistics as S
import pathlib; A = str(pathlib.Path(__file__).resolve().parents[2])
def load(host,part):
  d=collections.defaultdict(list)
  for r in csv.DictReader(open(f"{A}/results/{host}/{part}.csv")):
    if r['ok']!='1': continue
    ex=dict(kv.split(':',1) for kv in r['extra'].split(';') if ':' in kv)
    fair=None
    if 'thr_min_s' in ex: fair=float(ex['thr_min_s'])/float(ex['thr_max_s'])
    d[(r['impl'],int(r['threads']),int(r['outside_work']),int(r['n']))].append((float(r['seconds'])/int(r['iters']),fair))
  return d
for host in ['LAP1184','apple-m5']:
  L=load(host,'lock')
  med=lambda k: S.median(x[0] for x in L[k]) if k in L else None
  fm=lambda k: S.median(x[1] for x in L[k]) if k in L else None
  print(host,'OW15 ttas/tas', [(p, round(med(('ttas',p,15,1009))/med(('tas',p,15,1009)),2)) for p in [2,3,4,6,8,10,12,16,20,24] if ('tas',p,15,1009) in L])
  for impl in ['tas_bo_sleep','tas']:
    print(host,impl,'fair OW0 N1009',[(p,round(fm((impl,p,0,1009)),3)) for p in [4,8,10,12,16,20,24] if (impl,p,0,1009) in L])
  C=load(host,'counter')
  for impl in ['cas_sleep','faa','lock_std_mutex','lock_pthread_mutex']:
    print(host,impl,'fair OW0',[(p,round(S.median(x[1] for x in C[(impl,p,0,101)]),3)) for p in [2,4,8,10,12,20,24] if (impl,p,0,101) in C])
