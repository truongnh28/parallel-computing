import csv,collections,statistics as S
import pathlib; A = str(pathlib.Path(__file__).resolve().parents[2])
for host in ['apple-m5','LAP1184']:
  d=collections.defaultdict(list); it=collections.defaultdict(list)
  for r in csv.DictReader(open(f"{A}/results/{host}/counter.csv")):
    if r['ok']!='1': continue
    ex=dict(kv.split(':',1) for kv in r['extra'].split(';') if ':' in kv)
    k=(r['impl'],int(r['threads']),int(r['outside_work']))
    d[k].append((float(r['seconds'])/int(r['ops']), float(ex.get('cas_fail',0))/int(r['ops'])))
    it[k].append(int(r['iters']))
  P=sorted({k[1] for k in d})
  for ow in [0,1,8]:
    for impl in ['cas','cas_seqcst','cas_yield','cas_spin','cas_sleep']:
      print(host,ow,impl,'f',[(p,round(S.median(x[1] for x in d[(impl,p,ow)]),3)) for p in P if (impl,p,ow) in d])
    for impl in ['faa','cas','lock_std_mutex','lock_pthread_mutex','lock_os_unfair_lock','lock_tas','lock_mcs']:
      if (impl,1,ow) in d: print(host,ow,impl,'ns/op',[(p,round(1e9*S.median(x[0] for x in d[(impl,p,ow)]),1)) for p in P if (impl,p,ow) in d])
  print(host,'mcs iters >10',[(p,ow,sorted(set(it[('lock_mcs',p,ow)]))) for p in P for ow in [0,1,8] if p>10 and ('lock_mcs',p,ow) in it])
