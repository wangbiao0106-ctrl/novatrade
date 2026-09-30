#!/usr/bin/env python3
"""数据预处理：解析 478 个合约 5m OHLCV，重采样为多周期，输出 npz + meta.csv"""
import argparse
import gzip, json, os, sys, glob
from pathlib import Path
import numpy as np
import pandas as pd
from concurrent.futures import ProcessPoolExecutor

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data/kline/okx/swap/5m"
DEFAULT_OUT_DIR = PROJECT_ROOT / "strategies/sweep_reversal_short/results/data"
TFS = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}   # 分钟
MIN_BARS = {"5m": 1000, "15m": 300, "30m": 150, "1h": 80, "4h": 30}

def parse_one(path):
    sym = os.path.basename(path).split("_USDT_SWAP_")[0]
    rows = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": [], "qv": []}
    with gzip.open(path, "rt") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            rows["t"].append(int(d["timestamp_ms"]))
            rows["o"].append(float(d["open"]))
            rows["h"].append(float(d["high"]))
            rows["l"].append(float(d["low"]))
            rows["c"].append(float(d["close"]))
            rows["v"].append(float(d.get("volume", 0) or 0))
            rows["qv"].append(float(d.get("quote_volume", 0) or 0))
    if not rows["t"]:
        return sym, None, None
    t = np.array(rows["t"], dtype=np.int64)
    o = np.array(rows["o"]); h = np.array(rows["h"]); l = np.array(rows["l"])
    c = np.array(rows["c"]); v = np.array(rows["v"]); qv = np.array(rows["qv"])
    # 去重、排序
    order = np.argsort(t, kind="stable")
    t, o, h, l, c, v, qv = (a[order] for a in (t, o, h, l, c, v, qv))
    _, idx = np.unique(t, return_index=True)
    t, o, h, l, c, v, qv = (a[idx] for a in (t, o, h, l, c, v, qv))
    # 元信息
    meta = dict(sym=sym, n=len(t), t0=t[0], t1=t[-1],
                tot_qv=float(qv.sum()), mean_vol=float(v.mean()),
                med_vol=float(np.median(v)))
    # 重采样
    out = {}
    for tf, mins in TFS.items():
        bin_ms = 60_000 * mins
        bins = (t // bin_ms) * bin_ms
        if len(np.unique(bins)) < MIN_BARS[tf]:
            continue
        df = pd.DataFrame({"bin": bins, "o": o, "h": h, "l": l, "c": c, "v": v, "qv": qv})
        g = df.groupby("bin", sort=True)
        # Input is 5-minute candles.  A partial bucket or a gap must not be
        # silently turned into a complete higher-timeframe candle; doing so
        # changes closes/volumes and can create a signal from unavailable data.
        expected_children = max(1, mins // 5)
        complete_bins = []
        for bucket, child in g:
            timestamps = sorted(int(value) for value in t[bins == bucket])
            expected = [int(bucket) + index * 300_000 for index in range(expected_children)]
            if timestamps == expected:
                complete_bins.append(bucket)
        if not complete_bins:
            continue
        df = df[df["bin"].isin(complete_bins)]
        g = df.groupby("bin", sort=True)
        r = pd.DataFrame({
            "t": g["bin"].first(),
            "o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(),
            "c": g["c"].last(), "v": g["v"].sum(), "qv": g["qv"].sum()})
        out[tf] = r[["t", "o", "h", "l", "c", "v", "qv"]]
    return sym, meta, out

def worker(paths):
    metas, all_out = [], {}
    for p in paths:
        try:
            sym, meta, out = parse_one(p)
        except Exception as e:
            print(f"[err] {os.path.basename(p)}: {e}", file=sys.stderr)
            continue
        if meta is None:
            continue
        metas.append(meta)
        for tf, df in (out or {}).items():
            all_out.setdefault(tf, {})[sym] = df
    return metas, all_out

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 4))
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be positive")
    data_dir, output_dir = args.data_dir, args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = sorted(glob.glob(os.path.join(data_dir, "*_5m_*.jsonl.gz")))
    print(f"共 {len(paths)} 个文件", flush=True)
    nw = min(args.workers, len(paths)) if paths else 1
    chunks = [paths[i::nw] for i in range(nw)]
    metas, all_out = [], {}
    with ProcessPoolExecutor(max_workers=nw) as ex:
        for m, o in ex.map(worker, chunks):
            metas.extend(m)
            for tf, d in o.items():
                all_out.setdefault(tf, {}).update(d)
    meta_df = pd.DataFrame(metas).sort_values("tot_qv", ascending=False)
    meta_df.to_csv(output_dir / "meta.csv", index=False)
    for tf, d in all_out.items():
        tdir = output_dir / tf
        os.makedirs(tdir, exist_ok=True)
        for sym, df in d.items():
            arr = df.to_numpy()   # t,o,h,l,c,v,qv
            np.savez_compressed(tdir / f"{sym}.npz",
                                arr=arr)
    print(f"meta: {len(metas)} 个合约")
    for tf in TFS:
        print(f"  {tf}: {len(all_out.get(tf, {}))} 个合约", flush=True)

if __name__ == "__main__":
    main()
