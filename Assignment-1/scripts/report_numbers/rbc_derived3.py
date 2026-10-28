# seq_cst ratios over all p. Source: results/apple-m5/counter.csv, median of ok=1 rows
import csv,statistics as st,sys,collections
import pathlib
A1=sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parents[2])
C=list(csv.DictReader(open(A1+"/results/apple-m5/counter.csv")))
t=collections.defaultdict(list)
for r in C:
    if r['ok']=='1': t[(r['impl'],int(r['outside_work']),int(r['threads']))].append(float(r['seconds'])*1e6/int(r['iters']))
m=lambda k: st.median(t[k])
P=[2,3,4,6,8,10,12,16,20]
for a,b in [('cas_seqcst','cas'),('faa_seqcst','faa')]:
    for ow in [0,1,8]:
        rs=[m((a,ow,p))/m((b,ow,p)) for p in P]; print(a,ow,[round(x,2) for x in rs],round(min(rs),2),round(max(rs),2))
