"""Smart-money concept detector.

Walks the bars of one symbol/timeframe from left to right and emits events at
the bar where they become *known* (no look-ahead): a swing high with k bars
on each side is only stamped k bars after the pivot, a sweep is stamped on the
bar that wicked through a pool and closed back, and so on.

Event vocabulary (field k):
  session_open   first bar of a regular session
  swing          confirmed swing point, labelled HH/LH (highs) or HL/LL (lows)
  pool_formed    resting liquidity: EQH/EQL clusters, previous-day high/low
  sweep          a pool was wicked through and the bar closed back inside
  pool_taken     a pool was broken with a close beyond it (no sweep)
  bos / mss      close beyond the last swing point; mss = against the trend
  fvg_formed     3-candle fair value gap; fvg_tapped / fvg_inverted afterwards
  ob_formed      order block: last opposite candle before a structure break;
                 ob_mitigated / ob_invalidated afterwards
"""
import numpy as np

from .data import TF_MIN


def atr(h, l, c, n=14):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    out = np.empty_like(tr)
    out[0] = tr[0]
    for i in range(1, len(tr)):
        out[i] = out[i - 1] + (tr[i] - out[i - 1]) / n
    return out


def r(x):
    return round(float(x), 4)


def detect(df, sym, tf, cfg):
    """df: bars with t,o,h,l,c,v,day,mins. Returns list of event dicts.

    Every event carries "i" (bar index where it became known); the build step
    strips it before export.
    """
    k = cfg["swing_k"]
    eq_tol, eq_look = cfg["eq_tol_atr"], cfg["eq_lookback"]
    fvg_min, fvg_age, ob_age = cfg["fvg_min_atr"], cfg["fvg_max_age"], cfg["ob_max_age"]
    pool_age = 2 * 390 // TF_MIN[tf]

    t = df.t.values.astype("int64")
    o, h, l, c = (df[x].values.astype(float) for x in "ohlc")
    day = df.day.values
    a = atr(h, l, c)
    n = len(df)

    ev = []

    def add(kind, i, **kw):
        e = {"id": f"{sym}:{tf}:{len(ev)}", "s": sym, "tf": tf, "k": kind, "t": int(t[i]), "i": i}
        e.update(kw)
        ev.append(e)
        return e

    highs, lows = [], []            # confirmed swings: (pivot index, price)
    last_sh = last_sl = None        # {"p": pivot idx, "price", "broken"}
    trend = 0                       # +1 after bullish break, -1 after bearish
    pools, fvgs, obs = [], [], []   # active objects (the event dicts themselves)
    day_hi = day_lo = None

    for i in range(n):
        # ---- new session: previous-day high/low become liquidity pools
        if i == 0 or day[i] != day[i - 1]:
            add("session_open", i, p=r(o[i]))
            if i > 0 and cfg.get("pdh_pdl_pools", True):
                for side, lvl, lb in (("BSL", day_hi, "PDH"), ("SSL", day_lo, "PDL")):
                    pools.append(add("pool_formed", i, side=side, lb=lb, p=r(lvl), t0=int(t[i]),
                                     t1=None, end=None, _born=i))
            day_hi, day_lo = h[i], l[i]
        else:
            day_hi, day_lo = max(day_hi, h[i]), min(day_lo, l[i])

        # ---- lifecycle of existing pools: sweep or take
        for pl in pools[:]:
            lvl = pl["p"]
            hit = h[i] > lvl if pl["side"] == "BSL" else l[i] < lvl
            if hit:
                closed_back = c[i] < lvl if pl["side"] == "BSL" else c[i] > lvl
                pl["t1"] = int(t[i])
                if closed_back:
                    pl["end"] = "swept"
                    add("sweep", i, d="bear" if pl["side"] == "BSL" else "bull", side=pl["side"],
                        lb=pl["lb"], p=lvl, x=r(h[i] if pl["side"] == "BSL" else l[i]),
                        vol=r(df.v.values[i] / max(1.0, np.mean(df.v.values[max(0, i - 20):i + 1]))),
                        ref=pl["id"])
                else:
                    pl["end"] = "taken"
                    add("pool_taken", i, side=pl["side"], lb=pl["lb"], p=lvl, ref=pl["id"])
                pools.remove(pl)
            elif i - pl["_born"] > pool_age:
                pl["t1"], pl["end"] = int(t[i]), "expired"
                pools.remove(pl)

        # ---- lifecycle of fair value gaps
        for g in fvgs[:]:
            if g["d"] == "bull":
                tapped, broken = l[i] <= g["top"], c[i] < g["bot"]
            else:
                tapped, broken = h[i] >= g["bot"], c[i] > g["top"]
            if broken:
                g["t1"], g["end"] = int(t[i]), "inverted"
                add("fvg_inverted", i, d="bear" if g["d"] == "bull" else "bull",
                    top=g["top"], bot=g["bot"], ref=g["id"])
                fvgs.remove(g)
                continue
            if tapped and not g["_tapped"]:
                g["_tapped"] = True
                add("fvg_tapped", i, d=g["d"], top=g["top"], bot=g["bot"], ref=g["id"])
            if i - g["_born"] > fvg_age:
                g["t1"], g["end"] = int(t[i]), "expired"
                fvgs.remove(g)

        # ---- lifecycle of order blocks
        for b in obs[:]:
            if b["d"] == "bull":
                touched, broken = l[i] <= b["top"], c[i] < b["bot"]
            else:
                touched, broken = h[i] >= b["bot"], c[i] > b["top"]
            if broken:
                b["t1"], b["end"] = int(t[i]), "invalidated"
                add("ob_invalidated", i, d=b["d"], top=b["top"], bot=b["bot"], ref=b["id"])
                obs.remove(b)
                continue
            if touched and not b["_touched"] and i > b["_born"]:
                b["_touched"] = True
                add("ob_mitigated", i, d=b["d"], top=b["top"], bot=b["bot"], ref=b["id"])
            if i - b["_born"] > ob_age:
                b["t1"], b["end"] = int(t[i]), "expired"
                obs.remove(b)

        # ---- swing confirmation (pivot p = i - k)
        if i >= 2 * k:
            p = i - k
            if h[p] > h[p - k:p].max() and h[p] >= h[p + 1:i + 1].max():
                lb = "" if not highs else ("HH" if h[p] > highs[-1][1] else "LH")
                add("swing", i, side="H", lb=lb or "H", p=r(h[p]), t0=int(t[p]))
                for q, price in reversed(highs):
                    if p - q > eq_look:
                        break
                    if abs(price - h[p]) <= eq_tol * a[i] and h[q + 1:p].max(initial=0) <= max(price, h[p]):
                        lvl = max(price, h[p])
                        if not any(pl["side"] == "BSL" and abs(pl["p"] - lvl) <= eq_tol * a[i] for pl in pools):
                            pools.append(add("pool_formed", i, side="BSL", lb="EQH", p=r(lvl),
                                             t0=int(t[q]), t1=None, end=None, _born=i))
                        break
                highs.append((p, h[p]))
                last_sh = {"p": p, "price": h[p], "broken": False}
            if l[p] < l[p - k:p].min() and l[p] <= l[p + 1:i + 1].min():
                lb = "" if not lows else ("LL" if l[p] < lows[-1][1] else "HL")
                add("swing", i, side="L", lb=lb or "L", p=r(l[p]), t0=int(t[p]))
                for q, price in reversed(lows):
                    if p - q > eq_look:
                        break
                    if abs(price - l[p]) <= eq_tol * a[i] and l[q + 1:p].min(initial=np.inf) >= min(price, l[p]):
                        lvl = min(price, l[p])
                        if not any(pl["side"] == "SSL" and abs(pl["p"] - lvl) <= eq_tol * a[i] for pl in pools):
                            pools.append(add("pool_formed", i, side="SSL", lb="EQL", p=r(lvl),
                                             t0=int(t[q]), t1=None, end=None, _born=i))
                        break
                lows.append((p, l[p]))
                last_sl = {"p": p, "price": l[p], "broken": False}

        # ---- structure breaks (+ order block of the breaking leg)
        for sw, d, sign in ((last_sh, "bull", 1), (last_sl, "bear", -1)):
            if sw is None or sw["broken"]:
                continue
            if (c[i] - sw["price"]) * sign > 0:
                sw["broken"] = True
                kind = "mss" if trend == -sign else "bos"
                trend = sign
                add(kind, i, d=d, p=r(sw["price"]), t0=int(t[sw["p"]]),
                    disp=r(abs(c[i] - o[i]) / a[i]))
                other = last_sl if sign == 1 else last_sh
                lo_j = other["p"] if other is not None and other["p"] < i else max(0, i - 20)
                for j in range(i - 1, max(lo_j, i - 30) - 1, -1):
                    if (c[j] < o[j]) if sign == 1 else (c[j] > o[j]):
                        obs.append(add("ob_formed", i, d=d, top=r(h[j]), bot=r(l[j]), t0=int(t[j]),
                                       t1=None, end=None, via=kind, _born=i, _touched=False))
                        break

        # ---- new fair value gap (bars i-2, i-1, i)
        if i >= 2:
            if l[i] > h[i - 2] and l[i] - h[i - 2] >= fvg_min * a[i]:
                fvgs.append(add("fvg_formed", i, d="bull", top=r(l[i]), bot=r(h[i - 2]), t0=int(t[i - 2]),
                                t1=None, end=None, _born=i, _tapped=False))
            elif h[i] < l[i - 2] and l[i - 2] - h[i] >= fvg_min * a[i]:
                fvgs.append(add("fvg_formed", i, d="bear", top=r(l[i - 2]), bot=r(h[i]), t0=int(t[i - 2]),
                                t1=None, end=None, _born=i, _tapped=False))

    return ev, a
