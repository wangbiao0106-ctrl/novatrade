#!/usr/bin/env python3
"""Reproducible EMA20/60/120 pullback research for four market directions."""
from __future__ import annotations

import argparse
import csv
import gzip
import itertools
import json
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data/kline/okx/swap/5m"
OUT = ROOT / "strategies/ema_3line_pullback/results"
UTC = timezone.utc
MAJOR = {"BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "TON", "AVAX", "LINK", "DOT", "LTC", "BCH", "ETC", "UNI", "ATOM", "NEAR", "APT", "SUI"}
NON_CRYPTO = {
    "AAOI", "AAPL", "ADBE", "AEHR", "ALAB", "AMAT", "AMC", "AMD", "AMZN", "ANTHROPIC", "APLD",
    "APP", "ARM", "ASML", "ASTS", "AVGO", "AXTI", "BB", "BE", "BRKB", "BX", "BZ", "CGNX",
    "CIEN", "CL", "COHR", "COIN", "COST", "CRCL", "CRDO", "CRM", "CRWD", "CRWV", "CSCO",
    "CSOPSAMSUNG2L", "CSOPSKHYNIX2L", "CXMT", "DDOG", "DELL", "DKNG", "EWJ", "EWT", "EWY",
    "EWZ", "FLNC", "FLY", "GEV", "GLW", "GME", "GOOGL", "GTLB", "HANMI", "HIMS", "HOOD", "HPE",
    "HUT", "HYUNDAI", "IBM", "INTC", "INTW", "IONQ", "IREN", "ISRG", "IWM", "JNJ", "JP225",
    "KIOXIA", "KLAC", "KO", "KORU", "KR200", "KSTR", "LGELECTRONICS", "LITE", "LLY", "LRCX",
    "LUNR", "LYTE", "MARA", "META", "MINIMAX", "MOONSHOT", "MRK", "MRNA", "MRVL", "MSFT",
    "MSTR", "MSTU", "MU", "MUU", "MVLL", "NAVER", "NBIS", "NET", "NFLX", "NG", "NOK", "NOW",
    "NVDA", "NVDL", "OKLO", "OKTA", "ON", "ONDS", "OPENAI", "ORCL", "OSCR", "OURA", "OUST",
    "PLTR", "POET", "POPMART", "PYPL", "QCOM", "QQQ", "RDDT", "RDW", "RIOT", "RIVN", "RKLB",
    "ROK", "SAMSUNG", "SHAZ", "SHEIN", "SHLD", "SHOP", "SIMO", "SKDD", "SKHY", "SKHYNIX",
    "SKUU", "SMCI", "SMH", "SNDK", "SNOW", "SOFTBANK", "SONY", "SOXL", "SOXS", "SPCH", "SPCX",
    "SPY", "SQQQ", "STABLE", "SUSD", "TEAM", "TEM", "TER", "TMF", "TQQQ", "TSEM", "TSLA",
    "TSLL", "TSM", "TTMI", "TTWO", "TWLO", "UNH", "UNITREE", "URNM", "US100", "US500", "USDF",
    "USDP", "USDY", "USO", "UVXY", "VRT", "WDC", "WEN", "WMT", "XAG", "XAU", "XBI", "XCU",
    "XIAOMI", "XLE", "XOM", "XPD", "XPT", "ZHIPU", "ZHONGJI", "ZM",
}
STABLE = {"USDC", "USDT", "DAI", "BUSD", "FDUSD", "TUSD", "USDE", "USD1", "PYUSD", "GUSD", "EURT", "EURS"}
INDICATOR_CACHE: dict[str, tuple[list[float], list[float], list[float], list[float], list[float]]] = {}

@dataclass
class Bar:
    ts: int; o: float; h: float; l: float; c: float; q: float

@dataclass
class Trade:
    variant: str; symbol: str; entry_ts: int; exit_ts: int; side: str
    entry: float; exit: float; stop: float; target: float; r: float; reason: str

def ema(xs: list[float], n: int) -> list[float]:
    a = 2 / (n + 1); out = []
    for x in xs: out.append(x if not out else a*x + (1-a)*out[-1])
    return out

def atr(bs: list[Bar], n: int = 14) -> list[float]:
    tr=[]
    for i,b in enumerate(bs):
        p=bs[i-1].c if i else b.c
        tr.append(max(b.h-b.l, abs(b.h-p), abs(b.l-p)))
    return ema(tr,n)

def resample(rows: list[Bar]) -> list[Bar]:
    groups: dict[int,list[Bar]] = {}
    for b in rows:
        hour=(b.ts//3_600_000)*3_600_000; groups.setdefault(hour,[]).append(b)
    out=[]
    for ts, g in sorted(groups.items()):
        g.sort(key=lambda x:x.ts)
        if len(g) < 12: continue
        out.append(Bar(ts,g[0].o,max(x.h for x in g),min(x.l for x in g),g[-1].c,sum(x.q for x in g)))
    return out

def load(path: Path) -> list[Bar]:
    out=[]
    with gzip.open(path,"rt") as f:
        for line in f:
            x=json.loads(line)
            if not x.get("confirmed",True): continue
            try: out.append(Bar(int(x["timestamp_ms"]),float(x["open"]),float(x["high"]),float(x["low"]),float(x["close"]),float(x.get("quote_volume",0))))
            except (KeyError,TypeError,ValueError): pass
    return resample(out)

def base(path: Path) -> str: return path.name.split("_USDT_")[0]
def eligible(path: Path, universe: str) -> bool:
    b=base(path)
    if b in STABLE or b in NON_CRYPTO: return False
    return (b in MAJOR) if universe == "major" else (b not in MAJOR)

def simulate(bs: list[Bar], name: str, p: dict, start: int, end: int, fee: float, slip: float, btc_state: dict[int, tuple[float, float, float, float]] | None = None) -> list[Trade]:
    if len(bs)<160 or end-start < 2: return []
    closes=[b.c for b in bs]
    # 缓存键必须是稳定的标的名（调用方传 base(path)）：`id(bs)` 只保证对象存活
    # 期间唯一，列表被回收后地址复用会让另一个品种取到本品种的指标。
    key=name
    if key not in INDICATOR_CACHE:
        q=[]; running=0.0; prefix=[0.0]
        for b in bs: prefix.append(prefix[-1]+b.q)
        for i in range(len(bs)):
            lo=max(0,i-47); q.append((prefix[i+1]-prefix[lo])/max(i-lo+1,1))
        INDICATOR_CACHE[key]=(ema(closes,20),ema(closes,60),ema(closes,120),atr(bs),q)
    e20,e60,e120,aa,qmean=INDICATOR_CACHE[key]
    trades=[]; pending=None; pos=None; i=max(125,start)
    side=p["side"]
    while i < min(end,len(bs)-1):
        b=bs[i]
        # BTC 门控只关闭该时刻的新信号（STRATEGY.md §3.5）。已持仓头寸的止损/
        # 止盈/超时必须每个小时照常判定，否则保护单会被门控整段暂停，并把止损
        # 推迟到门控重开后才按从未成交的止损价记账。
        gated=False
        if p.get("btc_gate"):
            state=btc_state.get(b.ts) if btc_state is not None else None
            if state is None:
                gated=True
            else:
                btc_close,btc_ema60,btc_ema60_3,btc_ema60_6=state
                btc_ok=btc_close > btc_ema60 if p["btc_gate"]=="above" else btc_close < btc_ema60
                slope=int(p.get("btc_slope_bars",0))
                if slope == 3: btc_ok = btc_ok and btc_ema60 > btc_ema60_3
                if slope >= 6: btc_ok = btc_ok and btc_ema60 > btc_ema60_6
                if not btc_ok: gated=True
        if gated: pending=None
        if pos:
            hit_stop = b.l <= pos["stop"] if side=="long" else b.h >= pos["stop"]
            hit_target = b.h >= pos["target"] if side=="long" else b.l <= pos["target"]
            if hit_stop or hit_target:
                # conservative when both levels occur inside one bar; 跳空穿越
                # 止损时按更差的开盘价成交，不按未成交的止损价记账。
                if hit_stop:
                    px = min(pos["stop"], b.o) if side=="long" else max(pos["stop"], b.o)
                    reason = "stop"
                else:
                    px = pos["target"]
                    reason = "target"
                gross=(px-pos["entry"] if side=="long" else pos["entry"]-px)
                net=gross - (pos["entry"]+px)*fee - (pos["entry"]+px)*slip
                trades.append(Trade(name, "", pos["ts"], b.ts, side, pos["entry"], px, pos["stop"], pos["target"], net/pos["risk"], reason)); pos=None
            elif i-pos["index"] >= p["max_hold_bars"]:
                px=b.c; gross=(px-pos["entry"] if side=="long" else pos["entry"]-px); net=gross-(pos["entry"]+px)*fee-(pos["entry"]+px)*slip
                trades.append(Trade(name,"",pos["ts"],b.ts,side,pos["entry"],px,pos["stop"],pos["target"],net/pos["risk"],"timeout")); pos=None
            i+=1; continue
        if gated:
            i+=1; continue
        aligned=(closes[i]>e20[i]>e60[i]>e120[i]) if side=="long" else (closes[i]<e20[i]<e60[i]<e120[i])
        vol_ok=aa[i]/max(closes[i],1e-12)>=p["min_atr_pct"]
        slope=int(p.get("ema_slope_bars",0))
        ema_slope_ok=True if slope==0 or i<slope else (e60[i]>e60[i-slope] if side=="long" else e60[i]<e60[i-slope])
        volume_ratio=b.q/max(qmean[i],1e-12)
        volume_ok=volume_ratio>=p.get("min_volume_ratio",0.0)
        if pending:
            if i-pending > p["pullback_bars"] or not aligned: pending=None
            else:
                touched=(b.l <= e20[i]+p["pullback_atr"]*aa[i]) if side=="long" else (b.h >= e20[i]-p["pullback_atr"]*aa[i])
                held=(b.c>e20[i]) if side=="long" else (b.c<e20[i])
                if touched and held and vol_ok and ema_slope_ok and volume_ok:
                    en=bs[i+1].o*(1+slip if side=="long" else 1-slip); risk=max(p["stop_atr"]*aa[i], en*0.002)
                    stop=en-risk if side=="long" else en+risk; target=en+p["target_r"]*risk if side=="long" else en-p["target_r"]*risk
                    pos={"entry":en,"risk":risk,"stop":stop,"target":target,"ts":bs[i+1].ts,"index":i+1}; pending=None; i+=1; continue
        hi=max(x.h for x in bs[max(0,i-p["breakout_bars"]):i]); lo=min(x.l for x in bs[max(0,i-p["breakout_bars"]):i])
        prev_spread=(max(e20[i-1],e60[i-1],e120[i-1])-min(e20[i-1],e60[i-1],e120[i-1]))/max(aa[i-1],1e-12)
        breakout=(b.c>hi) if side=="long" else (b.c<lo)
        breakout_strength=((b.c-hi)/max(aa[i],1e-12)) if side=="long" else ((lo-b.c)/max(aa[i],1e-12))
        spread_now=(max(e20[i],e60[i],e120[i])-min(e20[i],e60[i],e120[i]))/max(aa[i],1e-12)
        if (prev_spread<=p["cluster_atr"] and breakout and aligned and vol_ok and ema_slope_ok and volume_ok
                and breakout_strength>=p.get("min_breakout_atr",0.0) and spread_now>=p.get("min_spread_atr",0.0)):
            pending=i
        i+=1
    return trades

def stats(ts: list[Trade]) -> dict:
    rs=[t.r for t in ts]; wins=[r for r in rs if r>0]; losses=[r for r in rs if r<=0]; eq=peak=dd=0; streak=best=0
    for r in rs:
        eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq); streak=streak+1 if r<=0 else 0; best=max(best,streak)
    return {"trades":len(rs),"wins":len(wins),"win_rate":round(len(wins)/len(rs),4) if rs else 0,"total_r":round(sum(rs),4),"avg_r":round(sum(rs)/len(rs),4) if rs else 0,"profit_factor":round(sum(wins)/abs(sum(losses)),4) if losses and sum(losses) else None,"max_drawdown_r":round(dd,4),"max_consecutive_losses":best}

def grid(basep: dict):
    if basep.get("universe") == "alt" and basep.get("side") == "long":
        keys=["btc_slope_bars","ema_slope_bars","min_breakout_atr","min_volume_ratio","min_spread_atr"]
        vals=[[0,3,6],[0,3,6],[0.0,0.15,0.3],[0.0,1.0,1.25],[0.0,0.25,0.5]]
        for v in itertools.product(*vals):
            p=dict(basep); p["target_r"]=2.5; p.update(dict(zip(keys,v))); yield p
        return
    keys=["cluster_atr","breakout_bars","pullback_atr","stop_atr","target_r","btc_slope_bars","ema_slope_bars","min_breakout_atr","min_volume_ratio","min_spread_atr"]
    vals=[[0.75,1.0],[3,4],[0.25,0.35],[1.25,1.5],[1.5,2.0,2.5,3.0],[0],[0],[0.0],[0.0],[0.0]]
    for v in itertools.product(*vals):
        p=dict(basep); p.update(dict(zip(keys,v))); yield p

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--optimize",action="store_true"); ap.add_argument("--max-symbols",type=int,default=0, help="cap test universe; default uses all eligible symbols"); ap.add_argument("--optimize-symbols",type=int,default=50, help="largest-volume symbols used for parameter selection"); args=ap.parse_args()
    cfg=json.loads((ROOT/"strategies/ema_3line_pullback/config/variants.json").read_text()); files=list(DATA.glob("*_USDT_SWAP_5m_*.jsonl.gz")); files=[f for f in files if base(f) not in STABLE and base(f) not in NON_CRYPTO]
    series={base(f): load(f) for f in files}
    # 训练/测试切分必须确定性：此前取 glob 第一个品种的首根 K 线，文件顺序或某个
    # 品种首个不完整小时都会让边界整体平移。这里用全池的时间范围。
    starts=[bars[0].ts for bars in series.values() if bars]; ends=[bars[-1].ts for bars in series.values() if bars]
    if not starts: raise SystemExit("no bars loaded")
    first,last=min(starts),max(ends); split=first+120*24*3600*1000; OUT.mkdir(parents=True,exist_ok=True); all_rows=[]; chosen={}
    btc_state={}
    if "BTC" in series:
        btc=series["BTC"]; b60=ema([b.c for b in btc],60)
        btc_state={b.ts: (b.c,b60[i],b60[i-3] if i>=3 else b60[i],b60[i-6] if i>=6 else b60[i]) for i,b in enumerate(btc)}
    detail_rows=[]; universe_rows={}
    for name, original in cfg["variants"].items():
        fs=[f for f in files if eligible(f,original["universe"])]
        fs.sort(key=lambda f: sum(b.q for b in series[base(f)] if b.ts < split), reverse=True)
        limit=original.get("max_symbols", 0) or args.max_symbols
        if limit: fs=fs[:limit]
        opt_fs=fs[:args.optimize_symbols]
        universe_rows[name]=[base(f) for f in fs]
        p=original
        if args.optimize:
            candidates=[]
            for candidate in grid(original):
                tr=[]
                for f in opt_fs:
                    bs=series[base(f)]; cut=next((i for i,b in enumerate(bs) if b.ts>=split),len(bs)); tr += simulate(bs,base(f),candidate,0,cut,cfg["fee_rate"],cfg["slippage"],btc_state)
                s=stats(tr); candidates.append((s["total_r"] if s["trades"]>=30 else -999,s["avg_r"],candidate,s))
            if name == "alt_long":
                candidates.sort(key=lambda x: (x[3]["trades"] >= 60 and x[3]["win_rate"] >= 0.40, x[3]["win_rate"], x[3]["avg_r"], x[3]["total_r"]), reverse=True)
            else:
                candidates.sort(key=lambda x:(x[0],x[1]),reverse=True)
            p=candidates[0][2] if candidates else p; chosen[name]={"params":p,"train_grid_best":candidates[0][3] if candidates else {}}
        test=[]; train=[]
        for f in fs:
            bs=series[base(f)]; cut=next((i for i,b in enumerate(bs) if b.ts>=split),len(bs)); a=simulate(bs,base(f),p,0,cut,cfg["fee_rate"],cfg["slippage"],btc_state); b=simulate(bs,base(f),p,cut,len(bs),cfg["fee_rate"],cfg["slippage"],btc_state); train+=a; test+=b
        for label, trades in (("train",train),("test",test)):
            s=stats(sorted(trades,key=lambda t:t.exit_ts)); s.update({"variant":name,"split":label,"params":p,"symbols":len(fs)}); all_rows.append(s)
            by_symbol={}
            for t in trades: by_symbol.setdefault(t.symbol, []).append(t.r)
            for symbol, rs in sorted(by_symbol.items()):
                detail_rows.append({"variant":name,"split":label,"symbol":symbol,"trades":len(rs),"wins":sum(r>0 for r in rs),"total_r":round(sum(rs),4),"avg_r":round(sum(rs)/len(rs),4)})
            for t in trades: t.symbol=base(next((f for f in fs if base(f)==t.symbol),fs[0]) if fs else fs[0]) if t.symbol else "" # retained for schema
    (OUT/"summary.json").write_text(json.dumps({"generated_at":datetime.now(UTC).isoformat(),"data_start":datetime.fromtimestamp(first/1000,UTC).isoformat(),"data_end":datetime.fromtimestamp(last/1000,UTC).isoformat(),"summary":all_rows,"chosen":chosen,"universe":universe_rows},ensure_ascii=False,indent=2))
    with (OUT/"summary.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=["variant","split","symbols","trades","wins","win_rate","total_r","avg_r","profit_factor","max_drawdown_r","max_consecutive_losses"], lineterminator="\n"); w.writeheader(); [w.writerow({k:x.get(k) for k in w.fieldnames}) for x in all_rows]
    with (OUT/"by_symbol.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=["variant","split","symbol","trades","wins","total_r","avg_r"], lineterminator="\n"); w.writeheader(); w.writerows(detail_rows)
    print(json.dumps({"data_start":datetime.fromtimestamp(first/1000,UTC).isoformat(),"data_end":datetime.fromtimestamp(last/1000,UTC).isoformat(),"summary":all_rows,"chosen":chosen},ensure_ascii=False,indent=2))

if __name__=="__main__": main()
