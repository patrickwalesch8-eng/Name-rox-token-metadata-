"""Bar storage, download (Yahoo Finance) and synthetic demo data.

All bars are regular-session (09:30-16:00 New York) 5-minute bars stored as
CSV with columns t,o,h,l,c,v where t is the bar open time in unix seconds.
Higher timeframes are aggregated from the 5m bars so every timeframe lines up.
"""
import time
from pathlib import Path

import numpy as np
import pandas as pd

NY = "America/New_York"
COLS = ["t", "o", "h", "l", "c", "v"]
TF_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60}
SESSION_OPEN_MIN = 9 * 60 + 30
SESSION_CLOSE_MIN = 16 * 60


def data_dir(root, demo=False):
    return Path(root) / "data" / ("demo" if demo else "raw")


def csv_path(root, sym, demo=False):
    return data_dir(root, demo) / f"{sym}_5m.csv"


def load_bars(root, sym, demo=False):
    p = csv_path(root, sym, demo)
    if not p.exists():
        return None
    df = pd.read_csv(p)
    return df[COLS].sort_values("t").reset_index(drop=True)


def save_bars(root, sym, df, demo=False):
    """Merge new bars into the stored file, so history grows with every fetch."""
    p = csv_path(root, sym, demo)
    p.parent.mkdir(parents=True, exist_ok=True)
    old = load_bars(root, sym, demo)
    if old is not None and not demo:
        df = pd.concat([old, df])
    df = df.drop_duplicates("t", keep="last").sort_values("t")
    df.to_csv(p, index=False, float_format="%.4f")
    return len(df)


def fetch_yahoo(sym, period="60d"):
    """Download 5m regular-session bars. Yahoo serves at most ~60 days of 5m data."""
    import yfinance as yf

    hist = yf.Ticker(sym).history(period=period, interval="5m", prepost=False, auto_adjust=False)
    if hist is None or hist.empty:
        raise RuntimeError(f"keine Daten von Yahoo fuer {sym}")
    idx = hist.index.tz_convert(NY) if hist.index.tz is not None else hist.index.tz_localize(NY)
    mins = idx.hour * 60 + idx.minute
    keep = (mins >= SESSION_OPEN_MIN) & (mins < SESSION_CLOSE_MIN)
    hist, idx = hist[keep], idx[keep]
    df = pd.DataFrame({
        # unit-safe (pandas may store ns, us or s resolution)
        "t": ((idx.tz_convert("UTC") - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)).astype("int64"),
        "o": hist["Open"].values, "h": hist["High"].values,
        "l": hist["Low"].values, "c": hist["Close"].values,
        "v": hist["Volume"].fillna(0).astype("int64").values,
    }).dropna()
    # drop the still-forming bar when fetching during the session
    return df[df.t + 300 <= time.time()].reset_index(drop=True)


def make_demo(sym, days=60, seed=None, end=None, early_close=None):
    """Synthetic intraday random walk with volatility clustering and U-shaped volume.

    Only meant for trying the dashboard without internet - not real prices.
    """
    rng = np.random.default_rng(seed if seed is not None else int.from_bytes(sym.encode(), "little") % 2**32)
    start = {"SPY": 560, "QQQ": 480, "AAPL": 225, "NVDA": 130, "TSLA": 250}.get(sym, 100.0)
    end_day = (pd.Timestamp(end) if end is not None
               else pd.Timestamp.now(tz=NY).normalize().tz_localize(None) - pd.Timedelta(days=1))
    sessions = pd.bdate_range(end=end_day, periods=days)
    early_close = early_close or {}
    rows, price, vol = [], float(start), 0.0012
    for d in sessions:
        open_ts = pd.Timestamp(d.year, d.month, d.day, 9, 30, tz=NY)
        price *= 1 + rng.normal(0, 0.004)  # overnight gap
        drift = rng.normal(0, 0.0003)
        n_bars = 78
        if d.strftime("%Y-%m-%d") in early_close:
            hh, mm = early_close[d.strftime("%Y-%m-%d")].split(":")
            n_bars = (int(hh) * 60 + int(mm) - SESSION_OPEN_MIN) // 5
        for k in range(n_bars):
            vol = max(0.0005, 0.94 * vol + 0.06 * 0.0012 + rng.normal(0, 0.00015))
            ushape = 1.6 if k < 6 or k > 70 else 1.0
            o = price
            path = o * np.cumprod(1 + rng.normal(drift, vol * ushape / 2, 4))
            c = float(path[-1])
            h = max(o, float(path.max())) * (1 + abs(rng.normal(0, vol / 3)))
            l = min(o, float(path.min())) * (1 - abs(rng.normal(0, vol / 3)))
            v = int(rng.lognormal(10, 0.4) * ushape * (start / 100))
            t = int((open_ts + pd.Timedelta(minutes=5 * k)).tz_convert("UTC").timestamp())
            rows.append((t, o, h, l, c, v))
            price = c
    return pd.DataFrame(rows, columns=COLS)


def add_session_cols(df):
    """Adds day (YYYY-MM-DD New York) and mins (minutes since NY midnight)."""
    ts = pd.to_datetime(df.t, unit="s", utc=True).dt.tz_convert(NY)
    df = df.copy()
    df["day"] = ts.dt.strftime("%Y-%m-%d")
    df["mins"] = ts.dt.hour * 60 + ts.dt.minute
    return df


def aggregate(df5, tf):
    """Aggregate session 5m bars into tf bars anchored at 09:30 each day."""
    step = TF_MIN[tf]
    if step == 5:
        return df5.reset_index(drop=True)
    df = df5.copy()
    df["bucket"] = (df.mins - SESSION_OPEN_MIN) // step
    g = df.groupby(["day", "bucket"], sort=True)
    out = g.agg(t=("t", "first"), o=("o", "first"), h=("h", "max"), l=("l", "min"),
                c=("c", "last"), v=("v", "sum"), mins=("mins", "first")).reset_index()
    # bar time = bucket start, even if the first 5m bar of the bucket is missing
    out["t"] = out.t - (out.mins - (SESSION_OPEN_MIN + out.bucket * step)) * 60
    out["mins"] = SESSION_OPEN_MIN + out.bucket * step
    return out[["t", "o", "h", "l", "c", "v", "day", "mins"]]
