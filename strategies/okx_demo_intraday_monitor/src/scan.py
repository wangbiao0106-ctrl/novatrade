#!/usr/bin/env python3
"""Read-only, explicitly demo-only automation audit. No order mutations."""
import concurrent.futures as cf
import datetime as dt
import json
from pathlib import Path
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
START = dt.datetime.now(dt.timezone.utc)
DAY = int(START.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
OUT = ROOT/'results'/START.strftime('%Y%m%dT%H%M%SZ')
OUT.mkdir(parents=True, exist_ok=False)
RAW = {}
ERRORS = []

def stamp(ms):
    t = dt.datetime.fromtimestamp(int(ms)/1000, dt.timezone.utc)
    return t.strftime('%Y-%m-%d %H:%M:%S UTC')+' / '+t.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S Asia/Shanghai')

def cli(*args):
    key = ' '.join(args)
    try:
        p = subprocess.run(['okx','--demo','--profile','okx-demo','--json','--env',*args], capture_output=True,text=True,timeout=30)
        if p.returncode: raise ValueError('CLI nonzero exit '+str(p.returncode))
        j = json.loads(p.stdout)
        if j.get('env') != 'demo': raise ValueError('demo envelope not confirmed')
        data = j['data']
        if isinstance(data,dict) and data.get('code') not in (None,'0',0): raise ValueError('API nonzero code')
        if args == ('account','config'):
            data = [{k:x.get(k) for k in ('acctLv','label','perm','posMode')} for x in data]
        fetched_ms = time.time()*1000
        RAW[key] = {'fetchedAt':stamp(fetched_ms),'fetchedAtMs':fetched_ms,'env':'demo','profile':j.get('profile'),'data':data}
        return data
    except Exception as e:
        ERRORS.append({'request':key,'error':type(e).__name__+': '+str(e)[:120]})
        return []

def batch(jobs):
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        return list(ex.map(lambda a:cli(*a), jobs))

def local(route):
    try:
        # Token stays only in memory; no raw headers or credentials are persisted.
        token=Path('/Users/bill/Library/Application Support/NovaTrade/locald.token').read_text().strip()
        req=urllib.request.Request('http://127.0.0.1:8787'+route,headers={'Authorization':'Bearer '+token})
        with urllib.request.urlopen(req,timeout=15) as f:return json.load(f)
    except Exception as e:return {'error':type(e).__name__}

state={}
for name,args in [('config',('account','config')),('positions',('account','positions','--instType','SWAP')),('orders',('swap','orders')),('oco',('swap','algo','orders','--ordType','oco')),('conditional',('swap','algo','orders','--ordType','conditional')),('algoHistory',('swap','algo','orders','--history','--ordType','oco')),('orderHistory',('swap','orders','--history')),('positionHistory',('account','positions-history','--instType','SWAP','--limit','100')),('balance',('account','balance','USDT'))]:
    state[name]=cli(*args)
state['localAccount']=local('/api/v1/account')
state['localRisk']=local('/api/v1/risk')
state['localStrategies']=local('/api/v1/strategies')
print('Account/history captured',flush=True)
inst,tickers,ois=batch([('market','instruments','--instType','SWAP'),('market','tickers','SWAP'),('market','open-interest','--instType','SWAP')])
specs={i['instId']:i for i in inst if i.get('state')=='live' and i.get('ctType')=='linear' and i.get('settleCcy')=='USDT' and i.get('instCategory')=='1'}
oi={i['instId']:i for i in ois}
universe=[t for t in tickers if t['instId'] in specs]
hour=batch([('market','candles',t['instId'],'--bar','1H','--limit','60') for t in universe])
hours={t['instId']:c for t,c in zip(universe,hour)}

def metrics(c,bar,observed_ms=None):
    c=sorted([x for x in c if len(x)>8 and str(x[8])=='1'],key=lambda x:int(x[0]))
    if len(c)<25:return {'valid':False}
    close=[float(x[4]) for x in c]; prev=c[-21:-1]
    trs=[max(float(c[i][2])-float(c[i][3]),abs(float(c[i][2])-close[i-1]),abs(float(c[i][3])-close[i-1])) for i in range(1,len(c))]
    atr=sum(trs[-14:])/14; avgvol=sum(float(x[7]) for x in prev)/20
    observed_ms = time.time()*1000 if observed_ms is None else observed_ms
    age=observed_ms-int(c[-1][0])-bar
    gap=int(observed_ms//bar)*bar-int(c[-1][0])-bar
    # A closed bar stays current throughout the next bar. At a boundary allow
    # at most 60 seconds for confirmation; elapsed time alone is not feed lag.
    fresh=-2000<=age and (gap==0 or (gap==bar and age-bar<=60000))
    return {'valid':fresh,'missingClosedBars':max(0,gap//bar),'lastClose':close[-1],'lastConfirmedOpen':stamp(c[-1][0]),'aboveSMA20':close[-1]>sum(close[-20:])/20,'move24Pct':(close[-1]/close[-25]-1)*100,'move6Pct':(close[-1]/close[-7]-1)*100,'volumeRatio':float(c[-1][7])/avgvol if avgvol else 0,'atrPct':atr/close[-1]*100,'atr':atr,'previousHigh':max(float(x[2]) for x in prev),'previousLow':min(float(x[3]) for x in prev),'breakUp':close[-1]>max(float(x[2]) for x in prev),'breakDown':close[-1]<min(float(x[3]) for x in prev),'ageAfterCloseMs':age}

def candle_metrics(name,label,bar):
    response=RAW.get('market candles '+name+' --bar '+label+' --limit 60',{})
    return metrics(response.get('data',[]),bar,response.get('fetchedAtMs'))

rows=[]
for t in universe:
    name=t['instId']; c=hours[name]; day=[x for x in c if int(x[0])>=DAY]; opening=next((float(x[1]) for x in day if int(x[0])==DAY),None)
    last=float(t['last']); sod=float(t.get('sodUtc0') or 0); bid=float(t.get('bidPx') or 0); ask=float(t.get('askPx') or 0)
    o=oi.get(name,{})
    rows.append({'instId':name,'last':last,'utcPct':(last/sod-1)*100 if sod else None,'candleUtcPct':(last/opening-1)*100 if opening else None,'utcOpen':opening,'sodUtc0':sod,'utcQuoteVolume':sum(float(x[7]) for x in day),'turnover24h':float(t['volCcy24h'])*last,'oiUsd':float(o.get('oiUsd') or 0),'oiTs':stamp(o['ts']) if o.get('ts') else None,'tickerTs':stamp(t['ts']),'tickerAgeMs':time.time()*1000-int(t['ts']),'spreadPct':(ask-bid)/((ask+bid)/2)*100 if ask>0 and bid>0 else None,'hour':candle_metrics(name,'1H',3600000)})
valid=[r for r in rows if r['utcPct'] is not None]
gainers=sorted(valid,key=lambda r:r['utcPct'],reverse=True)[:10]
for field,label in [('turnover24h','turnoverRank'),('oiUsd','oiRank')]:
    for rank,r in enumerate(sorted(rows,key=lambda r:r[field],reverse=True),1):r[label]=rank
for r in rows:r['heatScore']=.6*r['turnoverRank']+.4*r['oiRank']
heat=sorted(rows,key=lambda r:r['heatScore'])[:20]
pool=list(dict.fromkeys(r['instId'] for r in gainers+heat))
jobs=[]
for n in pool:jobs.extend([('market','candles',n,'--bar','5m','--limit','60'),('market','candles',n,'--bar','15m','--limit','60'),('market','orderbook',n,'--sz','20'),('market','funding-rate',n)])
results=batch(jobs)
by={n:{'5m':results[i*4],'15m':results[i*4+1],'book':results[i*4+2],'funding':results[i*4+3]} for i,n in enumerate(pool)}
four=batch([('market','candles',n,'--bar','4H','--limit','60') for n in ['BTC-USDT-SWAP','ETH-USDT-SWAP']])
overview={}
for n,c in zip(['BTC-USDT-SWAP','ETH-USDT-SWAP'],four):overview[n]={'5m':candle_metrics(n,'5m',300000),'15m':candle_metrics(n,'15m',900000),'1h':candle_metrics(n,'1H',3600000),'4h':candle_metrics(n,'4H',14400000),'funding':by[n]['funding'],'oi':oi.get(n)}

def slip(levels,spec):
    remaining=200.; weighted=0.; units=0.
    if not levels:return None
    top=float(levels[0][0])
    for level in levels:
        px=float(level[0]); size=float(level[1])*float(spec['ctVal'])*float(spec.get('ctMult') or 1)
        take=min(size,remaining/px); weighted+=px*take; units+=take; remaining-=px*take
        if remaining<1e-8:break
    return abs(weighted/units/top-1)*100 if remaining<1e-8 and units else None

checks=[]
for n in pool:
    r=next(r for r in rows if r['instId']==n); d=by[n]; m5=candle_metrics(n,'5m',300000); m15=candle_metrics(n,'15m',900000); m1=r['hour']; f=d['funding'][0] if d['funding'] else {}
    funding_valid=bool(f) and int(f.get('nextFundingTime') or 0)>time.time()*1000 and abs(time.time()*1000-int(f.get('ts') or 0))<120000
    book=d['book'][0] if d['book'] else {}; bids=book.get('bids',[]); asks=book.get('asks',[])
    spr=(float(asks[0][0])-float(bids[0][0]))/((float(asks[0][0])+float(bids[0][0]))/2)*100 if bids and asks else None
    buy=slip(asks,specs[n]); sell=slip(bids,specs[n]); reasons=[]
    if not funding_valid:reasons.append('资金费结算时间不可验证')
    if r['turnover24h']<3e6:reasons.append('24h成交额<300万')
    if spr is None or spr>.1:reasons.append('盘口价差>0.1%或缺失')
    if buy is None or sell is None or max(buy,sell)>.05:reasons.append('200USDT深度滑点>0.05%或不足')
    if not all(m.get('valid') for m in [m5,m15,m1]):reasons.append('K线缺失/延迟')
    if all(m.get('valid') for m in [m5,m15,m1]):
        aligned=all(m['aboveSMA20'] for m in [m5,m15,m1]) or all(not m['aboveSMA20'] for m in [m5,m15,m1])
        if not aligned:reasons.append('5m/15m/1h方向不一致')
        if not (m5['breakUp'] or m5['breakDown']) or m5['volumeRatio']<1.5:reasons.append('无放量突破确认')
    checks.append({'instId':n,'5m':m5,'15m':m15,'1h':m1,'funding':f,'fundingValid':funding_valid,'bookSpreadPct':spr,'buySlippagePct':buy,'sellSlippagePct':sell,'rejections':reasons})

# Actual SL count requires task provenance + actual SL algo + filled closing order + closed position.
task_ids={'3979314716284137482'}
task_orders={x['ordId'] for x in state['orderHistory'] if x.get('clOrdId','').startswith('codex')}
stops=[]
for a in state['algoHistory']:
    known=a.get('algoId') in task_ids or a.get('clOrdId','').startswith('codex') or a.get('algoClOrdId','').startswith('codex')
    if not known or a.get('actualSide')!='sl':continue
    ts=int(a.get('triggerTime') or a.get('uTime') or 0)
    if ts<DAY or ts>time.time()*1000:continue
    ords=a.get('ordIdList') or ([a['ordId']] if a.get('ordId') else [])
    closed=[p for p in state['positionHistory'] if p['instId']==a['instId'] and int(p['uTime'])>=ts and int(p['uTime'])-ts<120000 and float(p.get('closeTotalPos') or 0)>0]
    filled=[]
    for orderid in ords:
        detail=cli('swap','get','--instId',a['instId'],'--ordId',orderid)
        filled.extend(x for x in detail if x.get('state')=='filled' and float(x.get('accFillSz') or 0)>0)
    if filled and closed:stops.append({'algoId':a['algoId'],'instId':a['instId'],'triggerTime':stamp(ts),'closingOrders':filled,'closedPosition':closed})
state['positionsFinal']=cli('account','positions','--instType','SWAP')
state['ordersFinal']=cli('swap','orders')
state['ocoFinal']=cli('swap','algo','orders','--ordType','oco')
state['conditionalFinal']=cli('swap','algo','orders','--ordType','conditional')
summary={'runTime':stamp(START.timestamp()*1000),'finishedAt':stamp(time.time()*1000),'state':state,'utcStopCount':len({x['algoId'] for x in stops}),'stopEvidence':stops,'universe':rows,'gainers':gainers,'heat':heat,'candidatePool':pool,'benchmarks':overview,'candidateChecks':checks,'errors':ERRORS,'raw':RAW}
(OUT/'snapshot.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps({'output':str(OUT),'universe':len(rows),'pool':len(pool),'stopCount':summary['utcStopCount'],'errors':ERRORS,'positionsFinal':state['positionsFinal'],'ordersFinal':state['ordersFinal'],'ocoFinal':state['ocoFinal'],'benchmarks':overview},ensure_ascii=False),flush=True)
