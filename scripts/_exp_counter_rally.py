#!/usr/bin/env python3
"""Focused experiment: find a counter-rally detector that ACTUALLY cuts MaxDD<20%.
Tries trend-of-average, EMA-stack breadth, and persistence/hysteresis variants."""
import json, numpy as np, pandas as pd, math
from pathlib import Path

REPO = Path("/home/lunafox/binance-trade-bot")
syms = ['BTCUSDC','ETHUSDC','SOLUSDC','XRPUSDC','LINKUSDC']
prices={}
for s in syms:
    raw=json.loads((REPO/f"data/kline_cache/{s}_1h_4320.json").read_text())
    df=pd.DataFrame(raw); df['dt']=pd.to_datetime(df['ts'],unit='ms',utc=True)
    prices[s]=df.set_index('dt')['close'].astype(float)
px=pd.DataFrame(prices)
HOURS_PER_YEAR=8760
TAKER=0.0004; SLIP=0.0003; FEE=TAKER+SLIP
FUND8=0.00010; FUND_H=FUND8*3/24; FSIGN=-0.3

def equity_metrics(eq):
    eq=np.asarray(eq,float); n=len(eq)
    tot=eq[-1]-1; yrs=n/HOURS_PER_YEAR
    ann=(1+tot)**(1/yrs)-1 if (1+tot)>0 else -1
    r=np.diff(eq)/np.where(eq[:-1]>0,eq[:-1],np.nan); r=r[~np.isnan(r)]
    sh=(r.mean()/r.std(ddof=1))*math.sqrt(HOURS_PER_YEAR) if len(r)>2 and r.std(ddof=1)>0 else 0
    run=np.maximum.accumulate(eq); dd=(eq-run)/np.where(run>0,run,np.nan)
    mdd=abs(float(np.nanmin(dd))*100)
    return dict(ann=ann*100,sh=sh,mdd=mdd)

def sim_hold_short_with_flag(flag, leverage=1.0):
    """Buy&hold short across coins, flatten where flag True."""
    hr=px.pct_change().fillna(0)
    flag_arr = flag.reindex(px.index).fillna(False).to_numpy() if hasattr(flag,'reindex') else flag
    n=len(px); port=np.zeros(n); trades=0
    for ci,s in enumerate(px.columns):
        close=px[s].to_numpy(); pos=-1.0; eq=1.0; sr=np.zeros(n)
        for i in range(n):
            ret = close[i]/close[i-1]-1 if i>0 else 0
            notional=abs(pos)*eq*leverage
            fund = notional*FUND_H*FSIGN if pos<0 else (notional*FUND_H if pos>0 else 0)
            pnl=pos*eq*leverage*ret
            tgt = 0.0 if flag_arr[i] else -1.0
            if tgt!=pos:
                trades+=1; fc=abs(tgt-pos)*eq*leverage*FEE; pos=tgt
            else: fc=0
            p=eq; eq=eq+pnl-fund-fc
            if eq<=0: eq=0; pos=0
            sr[i]=eq/p-1 if p>0 else 0
        port+=sr/len(px.columns)
    eq=np.cumprod(1+port); m=equity_metrics(eq); m['trades']=trades
    return eq,m

# baseline
eq0,m0=sim_hold_short_with_flag(np.zeros(len(px),bool))
print(f'BASE hold-short: Ann {m0["ann"]:+.1f}% Sh {m0["sh"]:.2f} MDD {m0["mdd"]:.1f}% trades {m0["trades"]}')

# --- Detector ideas ---
def breadth(px,W,K,thr):
    rr=px.pct_change(W); return ((rr>thr).sum(axis=1)>=K).fillna(False).to_numpy()

def ema_stack(px,fast,slow):
    """Flag bar if >=K coins have fast EMA > slow EMA (individual uptrend)."""
    flags=[]
    for s in px.columns:
        ef=px[s].ewm(span=fast,adjust=False).mean()
        es=px[s].ewm(span=slow,adjust=False).mean()
        flags.append((ef>es).to_numpy())
    mat=np.vstack(flags).T
    return mat  # bool per (bar,coin)

def avg_trend(px,fast,slow):
    """Flag if the AVERAGE coin price's fast EMA > slow EMA."""
    avg=px.mean(axis=1)
    ef=avg.ewm(span=fast,adjust=False).mean()
    es=avg.ewm(span=slow,adjust=False).mean()
    return (ef>es).to_numpy()

def hysteresis(raw_flag, hold_on, hold_off=0):
    """Once on, stay on for hold_on bars; once off stay off for hold_off bars.
    Converts a noisy flag into a cleaner regime with debounce."""
    out=np.zeros(len(raw_flag),bool)
    on=False; countdown=0
    for i in range(len(raw_flag)):
        if raw_flag[i]:
            on=True; countdown=hold_on
        if on:
            out[i]=True; countdown-=1
            if countdown<=0: on=False
    return out

# Sweep EMA-stack breadth (K coins in individual uptrend)
print('\n=== EMA-stack breadth (K coins fast>slow) ===')
best=None
for fast in [12,24,48]:
    for slow in [48,96,168,240]:
        if fast>=slow: continue
        mat=ema_stack(px,fast,slow)
        for K in [3,4,5]:
            raw=(mat.sum(axis=1)>=K)
            for hold in [0,12,24,48]:
                flag=hysteresis(raw,hold)
                eq,m=sim_hold_short_with_flag(flag)
                tag=f'f{fast}/s{slow} K{K} hold{hold}'
                line=f'  {tag}: Ann {m["ann"]:+6.1f}% Sh {m["sh"]:5.2f} MDD {m["mdd"]:4.1f}% tr {m["trades"]:4d} fire {flag.mean()*100:.0f}%'
                if m['mdd']<20 and m['sh']>1 and m['ann']>50:
                    line+='  *** PASS ***'
                    if best is None or m['mdd']<best[1]['mdd']:
                        best=(tag,m,flag)
                print(line)

# Sweep avg-trend (portfolio-level)
print('\n=== Avg-coin trend filter (fast/slow EMA on mean price) ===')
for fast in [12,24,48]:
    for slow in [48,96,168,240]:
        if fast>=slow: continue
        raw=avg_trend(px,fast,slow)
        for hold in [0,12,24,48]:
            flag=hysteresis(raw,hold)
            eq,m=sim_hold_short_with_flag(flag)
            tag=f'f{fast}/s{slow} hold{hold}'
            line=f'  {tag}: Ann {m["ann"]:+6.1f}% Sh {m["sh"]:5.2f} MDD {m["mdd"]:4.1f}% tr {m["trades"]:4d} fire {flag.mean()*100:.0f}%'
            if m['mdd']<20 and m['sh']>1 and m['ann']>50:
                line+='  *** PASS ***'
                if best is None or m['mdd']<best[1]['mdd']:
                    best=(tag,m,flag)
            print(line)

# Combine avg-trend AND breadth thrust
print('\n=== Combo: avg-trend AND acute breadth thrust ===')
for tf,tfs in [(12,48),(24,96),(24,168),(48,168)]:
    trend=avg_trend(px,tf,tfs)
    for W,K,thr in [(6,5,0.02),(8,4,0.02),(12,4,0.02),(12,5,0.015),(24,4,0.03)]:
        br=breadth(px,W,K,thr)
        raw=trend & br
        for hold in [0,12,24,48,72]:
            flag=hysteresis(raw,hold)
            eq,m=sim_hold_short_with_flag(flag)
            tag=f'trend{tf}/{tfs}&brW{W}K{K}t{thr}hold{hold}'
            line=f'  {tag}: Ann {m["ann"]:+6.1f}% Sh {m["sh"]:5.2f} MDD {m["mdd"]:4.1f}% tr {m["trades"]:4d} fire {flag.mean()*100:.0f}%'
            if m['mdd']<20 and m['sh']>1 and m['ann']>50:
                line+='  *** PASS ***'
                if best is None or m['mdd']<best[1]['mdd']:
                    best=(tag,m,flag)
            print(line)

print('\n=== BEST ===')
if best:
    print(f'  {best[0]}: Ann {best[1]["ann"]:+.1f}% Sh {best[1]["sh"]:.2f} MDD {best[1]["mdd"]:.1f}% trades {best[1]["trades"]}')
    # save flag for the final script to use
    np.save('/tmp/best_flag.npy', best[2])
else:
    print('  NONE passed the gate.')
