"""The setup engine: turns detector events into trades - or refusals.

Chain (mirrored for longs):
  1. a buy-side pool (EQH/PDH) is swept -> look for a short
  2. within `mss_window` bars a bearish MSS/BOS confirms the reversal
  3. entry zone = newest bearish FVG of that leg, else the order block of the break
  4. a limit order waits `entry_window` bars at the zone edge
  5. stop beyond the sweep extreme, target at `target_rr` x risk, flat with the
     bar that ends at `flat_at` (default 15:55, so an alert still arrives before the bell)

Every candidate that does not become a trade is kept as a refusal with its
reason, so the farm can study what the engine passed on.
Fills are pessimistic: if stop and target are both inside one bar the stop
wins, and gaps never fill at a better price.
"""


def hhmm(s):
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm)


def htf_bias_series(df5, htf_events, tf_minutes):
    """Direction of the last higher-timeframe structure break known at each 5m bar close."""
    breaks = sorted((e["t"] + tf_minutes * 60, 1 if e["d"] == "bull" else -1)
                    for e in htf_events if e["k"] in ("bos", "mss"))
    out, j, cur = [], 0, 0
    for t in df5.t.values:
        close_t = int(t) + 300
        while j < len(breaks) and breaks[j][0] <= close_t:
            cur = breaks[j][1]
            j += 1
        out.append(cur)
    return out


def run_engine(df, events, atr_arr, sym, cfg, costs, bias=None, open_end=False, state_out=None):
    """df: 5m bars (t,o,h,l,c,v,day,mins); events: detector events of that df.

    open_end: the data ends at the newest *closed* bar of a running session (live
              trading) - the final bar is then not treated as the session's last bar.
    state_out: optional dict that receives the engine state after the final bar
               (pending "order", open "pos", waiting "cand") - used by the live bot.
    """
    t = df.t.values
    o, h, l, c = (df[x].values.astype(float) for x in "ohlc")
    day, mins = df.day.values, df.mins.values
    n = len(df)
    first_m, last_m = hhmm(cfg["no_entry_before"]), hhmm(cfg["no_entry_after"])
    flat_m = hhmm(cfg.get("flat_at", "16:00"))
    bar_min = 5

    by_i = {}
    for e in events:
        by_i.setdefault(e["i"], []).append(e)

    trades, refusals = [], []
    base_fields_keys = ("dir", "sweep", "sweep_t", "sweep_lb", "sweep_p", "sweep_x", "sweep_vol")
    st = {"cand": None, "order": None, "pos": None}
    per_day = {}

    def refuse(reason, i, base, **kw):
        rec = {"id": f"{sym}:R{len(refusals)}", "s": sym, "t": int(t[i]), "day": day[i], "reason": reason}
        if isinstance(base["sweep"], dict):     # a candidate
            rec.update(base_fields(base))
        else:                                   # a pending order already carries the fields
            rec.update({key: base[key] for key in base_fields_keys})
            rec.update({key: base[key] for key in ("mss", "mss_k", "mss_p", "mss_t", "entry", "sl", "tp", "armed_t")})
        rec.update(kw)
        refusals.append(rec)

    def cost_r(price, risk):
        per_side = price * costs["slippage_bps"] / 1e4 + costs["commission_per_share"]
        return 2 * per_side / risk

    def close(pos, i, px, why):
        s = 1 if pos["dir"] == "long" else -1
        gross = s * (px - pos["entry"]) / pos["risk"]
        pos.update(exit_t=int(t[i]), exit=round(px, 4), why=why, r=round(gross, 3),
                   net=round(gross - cost_r(pos["entry"], pos["risk"]), 3))
        trades.append(pos)

    def arm(base, brk, i, evs, last_bar):
        """MSS confirmed a candidate: pick the entry zone and place the order - or refuse."""
        short = base["dir"] == "short"
        want = "bear" if short else "bull"
        sw = base["sweep"]
        zone = None
        for e in reversed(events_between(base["i"], i)):
            if e["k"] == "fvg_formed" and e["d"] == want:
                zone = {"type": "FVG", "top": e["top"], "bot": e["bot"], "ref": e["id"]}
                break
        if zone is None:
            ob = next((e for e in evs if e["k"] == "ob_formed" and e["d"] == want), None)
            if ob is not None:
                zone = {"type": "OB", "top": ob["top"], "bot": ob["bot"], "ref": ob["id"]}
        info = {"mss": brk["id"], "mss_k": brk["k"], "mss_p": brk["p"], "mss_t": brk["t"]}
        if zone is None:
            return refuse("keine_zone", i, base, **info)
        info["zone"] = zone
        if last_bar:
            return refuse("sessionende", i, base, **info)
        if st["pos"] is not None or st["order"] is not None:
            return refuse("position_offen", i, base, **info)
        if per_day.get(day[i], 0) >= cfg["max_trades_per_day"]:
            return refuse("tageslimit", i, base, **info)
        if not first_m <= mins[i] <= last_m:
            return refuse("zeitfenster", i, base, **info)
        if cfg.get("use_htf_bias") and bias is not None and bias[i] == (1 if short else -1):
            return refuse("gegen_htf_trend", i, base, **info)

        a = atr_arr[i]
        entry = zone["bot"] if short else zone["top"]
        sl = sw["x"] + cfg["sl_buffer_atr"] * a if short else sw["x"] - cfg["sl_buffer_atr"] * a
        if (c[i] >= entry) if short else (c[i] <= entry):
            return refuse("zone_schon_erreicht", i, base, **info)
        risk = abs(sl - entry)
        if (sl <= entry) if short else (sl >= entry):
            return refuse("stop_ungueltig", i, base, **info)
        if risk < cfg["min_risk_atr"] * a:
            return refuse("risiko_zu_klein", i, base, **info)
        if risk > cfg["max_risk_atr"] * a:
            return refuse("risiko_zu_gross", i, base, **info)
        tp = entry - cfg["target_rr"] * risk if short else entry + cfg["target_rr"] * risk
        st["order"] = dict(base_fields(base), **info, s=sym, day=day[i], armed_t=int(t[i]),
                           entry=round(entry, 4), sl=round(sl, 4), tp=round(tp, 4),
                           risk=risk, expires=i + cfg["entry_window"])

    def base_fields(base):
        sw = base["sweep"]
        return {"dir": base["dir"], "sweep": sw["id"], "sweep_t": sw["t"], "sweep_lb": sw["lb"],
                "sweep_p": sw["p"], "sweep_x": sw["x"], "sweep_vol": sw.get("vol")}

    def events_between(i0, i1):
        out = []
        for j in range(i0, i1 + 1):
            out.extend(by_i.get(j, []))
        return out

    for i in range(n):
        # session end: last bar of the day, or the bar that ends at/after flat_at
        last_bar = (i == n - 1 and not open_end) or (i < n - 1 and day[i + 1] != day[i]) \
            or mins[i] + bar_min >= flat_m

        # 1) manage the open position (from the bar after the fill)
        pos = st["pos"]
        if pos is not None and i > pos["fill_i"]:
            s = 1 if pos["dir"] == "long" else -1
            if (l[i] <= pos["sl"]) if s == 1 else (h[i] >= pos["sl"]):
                close(pos, i, pos["sl"], "sl")
                st["pos"] = None
            elif (h[i] >= pos["tp"]) if s == 1 else (l[i] <= pos["tp"]):
                close(pos, i, pos["tp"], "tp")
                st["pos"] = None
            elif last_bar:
                close(pos, i, c[i], "eod")
                st["pos"] = None

        # 2) pending limit order (placed on an earlier bar)
        order = st["order"]
        if order is not None:
            s = 1 if order["dir"] == "long" else -1
            filled = (l[i] <= order["entry"]) if s == 1 else (h[i] >= order["entry"])
            ran_away = (h[i] >= order["tp"]) if s == 1 else (l[i] <= order["tp"])
            if filled:
                pos = dict(order, id=f"{sym}:T{len(trades)}", fill_t=int(t[i]), fill_i=i)
                per_day[day[i]] = per_day.get(day[i], 0) + 1
                st["order"] = None
                if (l[i] <= pos["sl"]) if s == 1 else (h[i] >= pos["sl"]):
                    close(pos, i, pos["sl"], "sl")      # same-bar stop: pessimistic
                elif last_bar:
                    close(pos, i, c[i], "eod")
                else:
                    st["pos"] = pos
            elif ran_away:
                refuse("lief_ohne_retest", i, order, zone=order["zone"])
                st["order"] = None
            elif i >= order["expires"] or last_bar:
                refuse("sessionende" if last_bar else "kein_retest", i, order, zone=order["zone"])
                st["order"] = None

        evs = by_i.get(i, [])

        # 3) a waiting candidate gets confirmed by an opposite structure break
        cand = st["cand"]
        if cand is not None:
            want = "bear" if cand["dir"] == "short" else "bull"
            brk = next((e for e in evs if e["k"] in ("mss", "bos") and e["d"] == want), None)
            if brk is not None:
                st["cand"] = None
                arm(cand, brk, i, evs, last_bar)
            elif i - cand["i"] >= cfg["mss_window"] or last_bar:
                refuse("kein_mss", i, cand)
                st["cand"] = None

        # 4) a fresh sweep starts a new candidate (not in the closing bars)
        for e in evs:
            if e["k"] == "sweep" and not last_bar:
                if st["cand"] is not None:
                    refuse("ersetzt", i, st["cand"])
                st["cand"] = {"dir": "short" if e["d"] == "bear" else "long", "sweep": e, "i": i}

    if state_out is not None:
        state_out.update(cand=st["cand"], order=st["order"], pos=st["pos"])
    for tr in trades:
        tr.pop("fill_i", None)
        tr.pop("expires", None)
        tr["risk"] = round(tr["risk"], 4)
    return trades, refusals
