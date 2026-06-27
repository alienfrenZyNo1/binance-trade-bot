#!/usr/bin/env python3
"""Finer robustness sweep around the passing breakout+trend config."""
import json, numpy as np, pandas as pd, math
from pathlib import Path
REPO=Path("/home/lunafox/binance-trade-bot")
syms=['BTCUSDC','ETHUSDC','SOLUSDC','XRPUSDC','LINKUSDC']
prices={}
for s in syms:
    raw=json.loads((REPO/f"data/kline_cache/{s}_1h_4320.json").read_text())
    df=pd.DataFrame(raw); df['dt']=pd.to_datetime(df['ts'],unit='ms',utc=True); prices[s]=df.set_index('dt')['close'].astype(float)
px=pd.DataFrame(prices)
FEE=0.0007; FH=0.00010*3/24; FS=-0.3
def em(x): pass
def metrics(eq,tr=0):
    eq=np.asarray(eq,float); n=len(eq); tot=eq[-1]-1; yrs=n/8760
    ann=(1+tot)**(1/yrs)-1 if (1+tot)>0 else -1
    r=np.diff(eq)/np.where(eq[:-1]>0,eq[:-1],np.nan); r=r[~np.isnan(r)]
    sh=(r.mean()/r.std(ddof=1))*math.sqrt(8760) if len(r)>2 and r.std(ddof=1)>0 else 0
    run=np.maximum.accumulate(eq); dd=(eq-run)/np.where(run>0,run,np.nan); mdd=abs(float(np.nanmin(dd))*100)
    return dict(ann=ann*100,sh=sh,mdd=mdd,tr=tr)
def sim(flag,lev=1.0):
    n=len(px); port=np.zeros(n); tr=0
    for s in px.columns:
        c=px[s].to_numpy(); pos=-1.0; eq=1.0; sr=np.zeros(n)
        for i in range(n):
            ret=c[i]/c[i-1]-1 if i>0 else 0; no=abs(pos)*eq*lev
            fu=no*FH*FS if pos<0 else (no*FH if pos>0 else 0); pnl=pos*eq*lev*ret
            tgt=0.0 if flag[i] else -1.0
            if tgt!=pos: tr+=1; fc=abs(tgt-pos)*eq*lev*FEE; pos=tgt
            else: fc=0
            p=eq; eq=eq+pnl-fu-fc
            if eq<=0: eq=0; pos=0
            sr[i]=eq/p-1 if p>0 else 0
        port+=sr/len(px.columns)
    return np.cumprod(1+port),metrics(np.cumprod(1+port),tr)
def hyst(raw,ho):
    out=np.zeros(len(raw),bool); on=False; co=0
    for i in range(len(raw)):
        if raw[i]: on=True; co=ho
        if on:
            out[i]=True; co-=1
            if co<=0: on=False
    return out
def avg_trend(fast,slow):
    a=px.mean(axis=1).ewm(span=fast,adjust=False).mean(); b=px.mean(axis=1).ewm(span=slow,adjust=False).mean(); return (a>b).to_numpy()
def breakout(W,frac,K=4):
    hi=px.rolling(W).max(); near=(px>=(1-frac)*hi).sum(axis=1); return (near>=K).to_numpy()

print("=== Fine hold sweep: t48/240 & brkW24 frac in {0.02,0.03,0.04,0.05}, hold in {60,72,84,96,108,120,144} ===")
print(f"{'frac':>5} {'hold':>5} | {'Ann':>7} {'Sh':>5} {'MDD':>5} {'tr':>4} {'fire%':>5} | PASS?")
passes=[]
for frac in [0.02,0.03,0.04,0.05]:
    for hold in [60,72,84,96,108,120,144]:
        t=avg_trend(48,240); b=breakout(24,frac); flag=hyst(t&b,hold)
        eq,m=sim(flag)
        p = m['mdd']<20 and m['sh']>1 and m['ann']>50
        if p: passes.append((frac,hold,m))
        print(f"{frac:>5.2f} {hold:>5} | {m['ann']:>+7.1f} {m['sh']:>5.2f} {m['mdd']:>5.1f} {m['tr']:>4} {flag.mean()*100:>5.0f} | {'PASS' if p else ''}")
print(f"\nPassing (frac,hold) pairs: {[(round(f,2),h) for f,h,_ in passes]}")
# count how many of the 28 combos pass
print(f"Pass count: {len(passes)}/28")

# also test K and W for breakout at the good hold
print("\n=== Around best: vary breakout W and K at frac=0.03-0.05, hold=96-108 ===")
for W in [18,24,30,36]:
    for K in [3,4,5]:
        for frac in [0.03,0.05]:
            for hold in [96,108]:
                t=avg_trend(48,240); b=breakout(W,frac,K); flag=hyst(t&b,hold)
                eq,m=sim(flag); p=m['mdd']<20 and m['sh']>1 and m['ann']>50
                print(f"  W{W} K{K} f{frac} h{hold}: Ann {m['ann']:+.1f} Sh {m['sh']:.2f} MDD {m['mdd']:.1f} tr {m['tr']} {'PASS' if p else ''}")
