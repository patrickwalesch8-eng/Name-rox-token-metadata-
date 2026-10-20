"""One symbol through the whole farm: detection on every timeframe + the engine.

Used by the dashboard build (history) and by the live MT5 bot (open_end=True).
"""
from . import data
from .detect import detect
from .engine import htf_bias_series, run_engine


def analyze(df5, sym, cfg, open_end=False):
    """df5: 5m session bars with day/mins columns (see data.add_session_cols).

    Returns dict with per_tf {tf: (bars, events, atr)}, trades, refusals, state
    (engine state after the final bar: pending order / open position / candidate).
    """
    per_tf = {}
    for tf in cfg["timeframes"]:
        dft = data.aggregate(df5, tf)
        ev, a = detect(dft, sym, tf, cfg["detect"])
        per_tf[tf] = (dft, ev, a)
    if "5m" in per_tf:
        df_base, ev_base, a_base = per_tf["5m"]
    else:
        df_base = df5.reset_index(drop=True)
        ev_base, a_base = detect(df_base, sym, "5m", cfg["detect"])
    bias = None
    if cfg["engine"].get("use_htf_bias", True):
        htf = per_tf.get("1h")
        if htf is None:
            dfh = data.aggregate(df5, "1h")
            htf = (dfh, detect(dfh, sym, "1h", cfg["detect"])[0], None)
        bias = htf_bias_series(df_base, htf[1], data.TF_MIN["1h"])
    state = {}
    trades, refusals = run_engine(df_base, ev_base, a_base, sym, cfg["engine"], cfg["costs"], bias,
                                  open_end=open_end, state_out=state)
    return {"per_tf": per_tf, "base": (df_base, ev_base, a_base), "trades": trades,
            "refusals": refusals, "state": state}
