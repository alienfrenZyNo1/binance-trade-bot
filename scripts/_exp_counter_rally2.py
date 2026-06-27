#!/usr/bin/env python3
"""Second experiment: test equity-DD control + verify best config robustness."""
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
FEE=0.0004+0.0003; FUND_H=0.00010*3/24; FSIGN=-0.3

def em(x,px,fast,slow):
    a=px[s].ewm(span=fast,adjust=False).mean(); b=px[s].ewm(span=slow,adjust=False).mean(); return (a>b).to_numpy()

def metrics(eq,tr=0):
    eq=np.asarray(eq,float); n=len(eq); tot=eq[-1]-1; yrs=n/8760
    ann=(1+tot)**(1/yrs)-1 if (1+tot)>0 else -1
    r=np.diff(eq)/np.where(eq[:-1]>0,eq[:-1],np.nan); r=r[~np.isnan(r)]
    sh=(r.mean()/r.std(ddof=1))*math.sqrt(8760) if len(r)>2 and r.std(ddof=1)>0 else 0
    run=np.maximum.accumulate(eq); dd=(eq-run)/np.where(run>0,run,np.nan); mdd=abs(float(np.nanmin(dd))*100)
    return dict(ann=ann*100,sh=sh,mdd=mdd,tr=tr)

def sim(flag, leverage=1.0):
    """flag: bool array len n. hold-short, flatten where flag."""
    n=len(px); port=np.zeros(n); trades=0
    for s in px.columns:
        close=px[s].to_numpy(); pos=-1.0; eq=1.0; sr=np.zeros(n)
        for i in range(n):
            ret=close[i]/close[i-1]-1 if i>0 else 0
            notional=abs(pos)*eq*leverage
            fund=notional*FUND_H*FSIGN if pos<0 else (notional*FUND_H if pos>0 else 0)
            pnl=pos*eq*leverage*ret
            tgt=0.0 if flag[i] else -1.0
            if tgt!=pos: trades+=1; fc=abs(tgt-pos)*eq*leverage*FEE; pos=tgt
            else: fc=0
            p=eq; eq=eq+pnl-fund-fc
            if eq<=0: eq=0; pos=0
            sr[i]=eq/p-1 if p>0 else 0
        port+=sr/len(px.columns)
    eq=np.cumprod(1+port); m=metrics(eq,trades); return eq,m

def hyst(raw,hold_on,hold_off=0):
    out=np.zeros(len(raw),bool); on=False; co=0; cfo=0
    for i in range(len(raw)):
        if raw[i]: on=True; co=hold_on
        if on:
            out[i]=True; co-=1
            if co<=0: on=False
    return out

def avg_trend(fast,slow):
    a=px.mean(axis=1).ewm(span=fast,adjust=False).mean(); b=px.mean(axis=1).ewm(span=slow,adjust=False).mean()
    return (a>b).to_numpy()

def breadth(W,K,thr):
    rr=px.pct_change(W); return ((rr>thr).sum(axis=1)>=K).fillna(False).to_numpy()

# ---- Portfolio equity drawdown control: flatten when portfolio equity is in DD > X% ----
# This needs to be computed online (no look-ahead) on the *short portfolio itself*.
def dd_control(max_dd_frac, cooldown):
    """Walk forward: maintain short; if running equity DD exceeds max_dd_frac,
    flatten for 'cooldown' bars then resume. No look-ahead."""
    n=len(px); hr=px.pct_change().fillna(0)
    port=np.zeros(n); trades=0
    for s in px.columns:
        close=px[s].to_numpy(); pos=-1.0; eq=1.0; peak=1.0; sr=np.zeros(n); cd=0
        for i in range(n):
            ret=close[i]/close[i-1]-1 if i>0 else 0
            notional=abs(pos)*eq
            fund=notional*FUND_H*FSIGN if pos<0 else (notional*FUND_H if pos>0 else 0)
            pnl=pos*eq*ret
            # decide target BEFORE applying this bar's move, using current eq/peak known up to i
            if cd>0:
                tgt=0.0; cd-=1
            else:
                cur_dd=(eq-peak)/peak if peak>0 else 0
                tgt = 0.0 if cur_dd < -max_dd_frac else -1.0
                if tgt==0.0: cd=cooldown
            if tgt!=pos: trades+=1; fc=abs(tgt-pos)*eq*FEE; pos=tgt
            else: fc=0
            p=eq; eq=eq+pnl-fund-fc
            if eq<=0: eq=0; pos=0
            peak=max(peak,eq)
            sr[i]=eq/p-1 if p>0 else 0
        port+=sr/len(px.columns)
    eq=np.cumprod(1+port); m=metrics(eq,trades); return eq,m

print("=== Portfolio equity DD control (flatten when short-book DD > X, cooldown Y) ===")
best=None
for maxdd in [0.08,0.10,0.12,0.15,0.18]:
    for cd in [24,48,72,96,120,168]:
        eq,m=dd_control(maxdd,cd)
        tag=f"ddstop{maxdd:.2f}_cd{cd}"
        line=f"  {tag}: Ann {m['ann']:+6.1f}% Sh {m['sh']:5.2f} MDD {m['mdd']:4.1f}% tr {m['tr']:4d}"
        if m['mdd']<20 and m['sh']>1 and m['ann']>50:
            line+="  *** PASS ***"
            if best is None or m['mdd']<best[1]['mdd']: best=(tag,m)
        print(line)

# ---- Verify the closest near-pass config robustness (neighbors) ----
print("\n=== Robustness of trend48/168 & brW12K5 thr1.5% hold72 (was MDD 21.5%) ===")
base=(48,168,12,5,0.015,72)
def build_flag(fast,slow,W,K,thr,hold):
    t=avg_trend(fast,slow); b=breadth(W,K,thr); return hyst(t&b,hold)
eq,mf=sim(build_flag(*base))
print(f"  BASE: Ann {mf['ann']:+.1f} Sh {mf['sh']:.2f} MDD {mf['mdd']:.1f}%")
# neighbors
nb=[(47,168,12,5,0.015,72),(48,168,11,5,0.015,72),(48,168,12,4,0.015,72),(48,168,12,5,0.012,72),(48,168,12,5,0.015,96)]
for p in nb:
    eq,mf=sim(build_flag(*p))
    print(f"  nb {p}: Ann {mf['ann']:+.1f} Sh {mf['sh']:.2f} MDD {mf['mdd']:.1f}%")

# ---- Try slightly more aggressive: trend+breakout(all coins near W-bar high) ----
print("\n=== Trend + breakout (avg coin within X% of rolling W-high) ===")
def breakout(px,W,frac):
    hi=px.rolling(W).max()
    # avg coin: fraction of coins within frac of their W-high
    near=(px>=(1-frac)*hi).sum(axis=1)
    return (near>=4).to_numpy()
for tf,ts in [(48,168),(24,168),(48,240)]:
    t=avg_trend(tf,ts)
    for W in [24,48,72]:
        for fr in [0.02,0.03,0.05]:
            b=breakout(px,W,fr); raw=t&b
            for hold in [48,72,96]:
                flag=hyst(raw,hold); eq,m=sim(flag)
                tag=f"t{tf}/{ts}&brkW{W}f{fr}h{hold}"
                line=f"  {tag}: Ann {m['ann']:+6.1f}% Sh {m['sh']:5.2f} MDD {m['mdd']:4.1f}% tr {m['tr']:4d} fire {flag.mean()*100:.0f}%"
                if m['mdd']<20 and m['sh']>1 and m['ann']>50:
                    line+="  *** PASS ***"
                    if best is None or m['mdd']<best[1]['mdd']: best=(tag,m)
                print(line)

print("\n=== BEST OVERALL ===")
if best: print(f"  {best[0]}: Ann {best[1]['ann']:+.1f}% Sh {best[1]['sh']:.2f} MDD {best[1]['mdd']:.1f}% tr {best[1]['tr']}")
else: print("  NONE cleared the gate.")
