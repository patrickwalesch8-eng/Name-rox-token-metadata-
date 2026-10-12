"""Runs detector + engine over all stored bars and writes the static dashboard."""
import json
import shutil
import time
from collections import Counter
from pathlib import Path

from . import data
from .detect import detect
from .engine import htf_bias_series, run_engine


def clean(e):
    return {k: v for k, v in e.items() if k != "i" and not k.startswith("_")}


def stats(trades):
    nets = [t["net"] for t in trades]
    if not nets:
        return {"n": 0}
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    eq = peak = dd = 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "n": len(nets),
        "win": round(len(wins) / len(nets), 3),
        "avg": round(sum(nets) / len(nets), 3),
        "net": round(sum(nets), 2),
        "gross": round(sum(t["r"] for t in trades), 2),
        "pf": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else None,
        "dd": round(dd, 2),
        "why": dict(Counter(t["why"] for t in trades)),
    }


def build(root, cfg, demo=False, log=print):
    root = Path(root)
    out = {"bars": {}, "events": [], "trades": [], "refusals": []}
    syms = []
    day_of = {}  # (symbol, bar time) -> New York session day
    for sym in cfg["symbols"]:
        raw = data.load_bars(root, sym, demo)
        if raw is None or raw.empty:
            log(f"  {sym}: keine Daten - erst 'fetch' (oder 'demo') ausfuehren")
            continue
        syms.append(sym)
        df5 = data.add_session_cols(raw)
        day_of.update({(sym, int(t)): d for t, d in zip(df5.t.values, df5.day.values)})
        out["bars"][sym] = {}
        per_tf = {}
        for tf in cfg["timeframes"]:
            dft = data.aggregate(df5, tf)
            ev, a = detect(dft, sym, tf, cfg["detect"])
            per_tf[tf] = (dft, ev, a)
            out["bars"][sym][tf] = [[int(r.t), round(r.o, 4), round(r.h, 4), round(r.l, 4), round(r.c, 4), int(r.v)]
                                    for r in dft.itertuples()]
        if "5m" in per_tf:
            df_base, ev_base, a_base = per_tf["5m"]
        else:
            df_base = df5
            ev_base, a_base = detect(df5, sym, "5m", cfg["detect"])
        bias = None
        htf = "1h" if "1h" in per_tf else None
        if htf:
            bias = htf_bias_series(df_base, per_tf[htf][1], data.TF_MIN[htf])
        trades, refusals = run_engine(df_base, ev_base, a_base, sym, cfg["engine"], cfg["costs"], bias)
        for tf, (_, ev, _) in per_tf.items():
            out["events"].extend(clean(e) for e in ev)
        out["trades"].extend(trades)
        out["refusals"].extend(refusals)
        log(f"  {sym}: {len(df5)} Bars, {sum(len(v[1]) for v in per_tf.values())} Events, "
            f"{len(trades)} Trades, {len(refusals)} abgelehnt")

    out["trades"].sort(key=lambda x: x["fill_t"])
    out["refusals"].sort(key=lambda x: x["t"])

    # daily studies: what happened per day and symbol
    studies = {}
    for e in out["events"]:
        d = _day_for(e["s"], e["t"], day_of)
        st = studies.setdefault(d, {}).setdefault(e["s"], {"counts": Counter(), "trades": [], "refusals": Counter()})
        st["counts"][f'{e["k"]}@{e["tf"]}'] += 1
    for tr in out["trades"]:
        studies.setdefault(tr["day"], {}).setdefault(tr["s"], {"counts": Counter(), "trades": [], "refusals": Counter()})["trades"].append(tr["id"])
    for rf in out["refusals"]:
        studies.setdefault(rf["day"], {}).setdefault(rf["s"], {"counts": Counter(), "trades": [], "refusals": Counter()})["refusals"][rf["reason"]] += 1
    for d in studies.values():
        for st in d.values():
            st["counts"], st["refusals"] = dict(st["counts"]), dict(st["refusals"])

    out["stats"] = {
        "all": stats(out["trades"]),
        "by_symbol": {s: stats([t for t in out["trades"] if t["s"] == s]) for s in syms},
        "by_dir": {d: stats([t for t in out["trades"] if t["dir"] == d]) for d in ("long", "short")},
        "refusals": dict(Counter(r["reason"] for r in out["refusals"])),
        "events": dict(Counter(e["k"] for e in out["events"])),
    }
    out["studies"] = studies
    out["meta"] = {
        "generated": int(time.time()),
        "demo": demo,
        "symbols": syms,
        "timeframes": cfg["timeframes"],
        "days": sorted(studies.keys()),
        "config": cfg,
    }

    site = root / "site"
    site.mkdir(exist_ok=True)
    for f in (root / "web").iterdir():
        shutil.copy(f, site / f.name)
    payload = json.dumps(out, separators=(",", ":"), default=_json_default)
    (site / "data.js").write_text("window.FARM=" + payload + ";\n", encoding="utf-8")
    log(f"Dashboard geschrieben: {site / 'index.html'}  ({len(payload) / 1e6:.1f} MB Daten)")
    return out


def _day_for(sym, t, day_of):
    d = day_of.get((sym, t))
    if d is not None:
        return d
    # higher-timeframe bar times are always a 5m bar time of the same session,
    # unless that 5m bar is missing - fall back to UTC-4/5 arithmetic
    return time.strftime("%Y-%m-%d", time.gmtime(t - 5 * 3600))


def _json_default(o):
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(type(o))
