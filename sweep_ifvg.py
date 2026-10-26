"""
Session sweep → broken FVG → retest entry. A discretionary ICT-style model written down as code.

Plain-words rules (short side; the long side is the mirror):
  1. Mark Asia (18:00 prev day – 03:00 ET) and London (03:00 – 09:00 ET) highs and lows.
  2. From 09:00 ET (NY session), price trades ABOVE an Asia or London high → the sweep.
  3. Find the most recent bullish fair value gap on the entry timeframe (1m or 5m)
     that formed on the push up (3-candle gap: candle 3's low is above candle 1's high).
  4. A candle CLOSES below that gap's bottom → the gap is broken.
  5. Entry: sell when price comes back up to the MIDDLE of the gap (fills from 09:30).
  6. Stop: 1 tick above the highest price since the sweep.
  7. Target, tested three ways:
       session  = nearest Asia/London low below entry ("opposite side")
       htf_gap  = nearest unfilled 1H/4H bullish gap below entry (first touch of its top)
       2R       = fixed 2x the risk
  One trade per day max (the first setup). Entries allowed 09:30–15:00, flat at 16:00.

Conservative assumptions: stop wins ties inside a bar; if the fill bar also reaches the
stop it's a loss; $1.24 round trip + 1 tick slip on stop/time exits; $200 risk sized in
MNQ, capped at 50 micros; if the setup's high/low is exceeded before the retest, that
setup is dropped and scanning continues.

Usage: python3 sweep_ifvg.py MNQ_5m.csv [MNQ_15m.csv for 1H/4H gaps] [--days N]
"""
import json
import math
import sys
from datetime import timedelta

import pandas as pd

PT_VALUE, TICK = 2.0, 0.25
FEE_RT, SLIP = 1.24, 0.25 * 2.0
FVG_LOOKBACK = 40
import os
SWEEP_START = os.environ.get("SWEEP_START", "09:00")  # when sweeps start counting (NY session)
MAX_TRADES = int(os.environ.get("MAX_TRADES", "1"))
RISK_USD = float(os.environ.get("RISK_USD", "200"))
MAX_CONTRACTS = int(os.environ.get("MAX_CONTRACTS", "50"))
FIXED_CONTRACTS = int(os.environ.get("FIXED_CONTRACTS", "0"))  # 0 = size from RISK_USD
BIAS = os.environ.get("BIAS", "none")  # none | 1h | 4h = only trade with that timeframe's trend (close vs 20 EMA)
FIB = os.environ.get("FIB", "none")  # none | pd | swing | overnight = range the 0.5 is drawn on; sweep must be in premium (shorts) / discount (longs)
CLOSE_PTS = float(os.environ.get("CLOSE_PTS", "30"))  # Asia & London levels this close → the sweep has to take both
ENTRY_END = os.environ.get("ENTRY_END", "15:00")  # no new entries at/after this time
FLAT_AT = os.environ.get("FLAT_AT", "16:00")  # open trades closed at this time
INV_MAX = int(os.environ.get("INV_MAX", "0"))  # gap must invert within N min of the sweep, else it's a continuation day (0 = off)
ENTRY_MODE = os.environ.get("ENTRY_MODE", "mid")  # cont = wait for price to tap the gap, enter on the first close back out of it  # mid = retest middle of gap; break = enter on the close of the breaking candle  # 2 = one more try after the first trade closes  # bars to look back for the gap that formed on the push into the sweep


def load(path):
    with open(path) as f:
        first = f.readline()
    if first.startswith("timestamp"):  # Dukascopy export (free, 24h Nasdaq 100 index CFD)
        d = pd.read_csv(path)
        d.index = pd.DatetimeIndex(pd.to_datetime(d.timestamp, unit="ms", utc=True)).tz_convert("America/New_York")
        d = d.rename(columns=str.capitalize)[["Open", "High", "Low", "Close"]].astype(float)
        return d[~d.index.duplicated()].sort_index()
    raw = pd.read_csv(path, header=[0, 1], index_col=0)
    raw.columns = [c[0] for c in raw.columns]
    df = raw[["Open", "High", "Low", "Close"]].astype(float).dropna()
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("America/New_York")
    return df[~df.index.duplicated()].sort_index()


def htf_gaps(df15, rule):
    """All FVGs on a resampled timeframe: (formed_at, kind, bottom, top)."""
    h = df15.resample(rule, origin="start_day", offset="18h").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    gaps = []
    for i in range(2, len(h)):
        a, c = h.iloc[i - 2], h.iloc[i]
        end = h.index[i] + pd.Timedelta(rule)  # usable once the 3rd candle has closed
        if c.Low > a.High:
            gaps.append((end, "bull", a.High, c.Low))
        elif c.High < a.Low:
            gaps.append((end, "bear", c.High, a.Low))
    return gaps


def htf_obs(df15, rule):
    """Order blocks: the last opposite-colour candle before a candle run that leaves an FVG.
    Same tuple shape as htf_gaps: (usable_from, kind, bottom, top)."""
    h = df15.resample(rule, origin="start_day", offset="18h").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    obs = []
    for i in range(3, len(h)):
        a, c = h.iloc[i - 2], h.iloc[i]
        end = h.index[i] + pd.Timedelta(rule)
        for k in range(i - 2, max(i - 6, 0), -1):
            ob = h.iloc[k]
            if c.Low > a.High and ob.Close < ob.Open:
                obs.append((end, "bull", ob.Low, ob.High))
                break
            if c.High < a.Low and ob.Close > ob.Open:
                obs.append((end, "bear", ob.Low, ob.High))
                break
    return obs


def unfilled_gap_target(gaps, df15, at, side, entry, max_days=10):
    """Nearest gap in the trade direction that price hasn't traded into since it formed."""
    best = None
    for formed, kind, bot, top in gaps:
        if formed > at or formed < at - pd.Timedelta(days=max_days):
            continue
        after = df15[(df15.index >= formed) & (df15.index < at)]
        if side == -1 and kind == "bull" and top < entry:
            if len(after) and after.Low.min() <= top:
                continue  # already touched
            lvl = top
        elif side == 1 and kind == "bear" and bot > entry:
            if len(after) and after.High.max() >= bot:
                continue
            lvl = bot
        else:
            continue
        if best is None or abs(lvl - entry) < abs(best - entry):
            best = lvl
    return best


def htf_bias(df15, rule):
    """Per closed HTF bar: +1 if close above its 20 EMA, -1 if below. Indexed by close time."""
    h = df15.resample(rule, origin="start_day", offset="18h").agg({"Close": "last"}).dropna()
    sign = (h.Close > h.Close.ewm(span=20, adjust=False).mean()).map({True: 1, False: -1})
    sign.index = sign.index + pd.Timedelta(rule)
    return sign


def sessions(df, d0):
    H = pd.Timedelta(hours=1)
    asia = df[(df.index >= d0 - 6 * H) & (df.index < d0 + 3 * H)]   # 18:00 prev day – 03:00
    lon = df[(df.index >= d0 + 3 * H) & (df.index < d0 + 9 * H)]     # 03:00 – 09:00
    if asia.empty or lon.empty:
        return None
    return {"Asia High": asia.High.max(), "Asia Low": asia.Low.min(),
            "London High": lon.High.max(), "London Low": lon.Low.min()}


def find_setup(bars, lv, bias=0, fib=None):
    """Walk the NY session; return the first filled setup or None."""
    highs = [lv["Asia High"], lv["London High"]]
    lows = [lv["Asia Low"], lv["London Low"]]
    up_lvl = max(highs) if abs(highs[0] - highs[1]) <= CLOSE_PTS else min(highs)  # close together → take both
    dn_lvl = min(lows) if abs(lows[0] - lows[1]) <= CLOSE_PTS else max(lows)
    mid = (fib[0] + fib[1]) / 2 if fib else None
    up_sweep = dn_sweep = None       # index of the first sweep bar
    pending = None                   # dict for a broken gap waiting for its retest
    idx = bars.index
    for i in range(2, len(bars)):
        b = bars.iloc[i]
        t = idx[i].time()
        if t < pd.Timestamp(SWEEP_START).time():
            continue
        if t >= pd.Timestamp(ENTRY_END).time() and pending is None:
            return None
        if up_sweep is None and b.High > up_lvl and (mid is None or b.High > mid):
            up_sweep = i
        if dn_sweep is None and b.Low < dn_lvl and (mid is None or b.Low < mid):
            dn_sweep = i

        # 1) a pending setup gets filled, or dropped if the extreme breaks first
        if pending and ENTRY_MODE == "cont":
            side, (g_bot, g_top) = pending["side"], pending["gap"]
            if (side == -1 and b.High > pending["extreme"]) or (side == 1 and b.Low < pending["extreme"]):
                pending = None
                continue
            if not pending.get("tapped") and ((side == -1 and b.High >= g_bot) or (side == 1 and b.Low <= g_top)):
                pending["tapped"] = True
            if pending.get("tapped") and t >= pd.Timestamp("09:30").time() and (
                    (side == -1 and b.Close < g_bot) or (side == 1 and b.Close > g_top)):
                return {**pending, "entry": b.Close, "fill_i": i}
            if t >= pd.Timestamp(ENTRY_END).time():
                return None
            continue
        if pending:
            side = pending["side"]
            if side == -1:
                if b.High > pending["extreme"]:
                    if b.High >= pending["entry"] and b.Open < pending["entry"]:
                        pass  # ran straight through entry and stop in one bar → treated as loss below
                    else:
                        pending = None
                        continue
                if b.High >= pending["entry"] and t >= pd.Timestamp("09:30").time():
                    return {**pending, "fill_i": i}
            else:
                if b.Low < pending["extreme"]:
                    if b.Low <= pending["entry"] and b.Open > pending["entry"]:
                        pass
                    else:
                        pending = None
                        continue
                if b.Low <= pending["entry"] and t >= pd.Timestamp("09:30").time():
                    return {**pending, "fill_i": i}
            if t >= pd.Timestamp(ENTRY_END).time():
                return None
            continue

        # 2) look for a broken gap after a sweep
        for side, sw in ((-1, up_sweep), (1, dn_sweep)):
            if sw is None or i <= sw or (bias and side != bias):
                continue
            if INV_MAX and idx[i] - idx[sw] > pd.Timedelta(minutes=INV_MAX):
                continue
            seg = bars.iloc[max(0, sw - FVG_LOOKBACK): i]  # push into the sweep + after it
            gap = None
            for k in range(len(seg) - 1, 1, -1):
                a, c = seg.iloc[k - 2], seg.iloc[k]
                if side == -1 and c.Low > a.High:
                    gap = (a.High, c.Low)   # bullish gap: bottom, top
                    break
                if side == 1 and c.High < a.Low:
                    gap = (c.High, a.Low)   # bearish gap: bottom, top
                    break
            if not gap:
                continue
            since = bars.iloc[sw: i + 1]
            # continuation entry: take the close of the candle that breaks the gap
            if ENTRY_MODE == "break" and t >= pd.Timestamp("09:30").time() and (
                    (side == -1 and b.Close < gap[0]) or (side == 1 and b.Close > gap[1])):
                lvl_kind = "High" if side == -1 else "Low"
                return {"side": side, "entry": b.Close, "gap": gap,
                        "extreme": since.High.max() if side == -1 else since.Low.min(),
                        "sweep_t": str(idx[sw]), "break_t": str(idx[i]), "fill_i": i,
                        "swept": [k for k, v in lv.items() if lvl_kind in k and
                                  ((bars.iloc[sw].High > v) if side == -1 else (bars.iloc[sw].Low < v))]}
            if side == -1 and b.Close < gap[0]:
                pending = {"side": -1, "entry": (gap[0] + gap[1]) / 2, "gap": gap, "extreme": since.High.max(),
                           "sweep_t": str(idx[sw]), "break_t": str(idx[i]),
                           "swept": [k for k, v in lv.items() if "High" in k and bars.iloc[sw].High > v]}
                break
            if side == 1 and b.Close > gap[1]:
                pending = {"side": 1, "entry": (gap[0] + gap[1]) / 2, "gap": gap, "extreme": since.Low.min(),
                           "sweep_t": str(idx[sw]), "break_t": str(idx[i]),
                           "swept": [k for k, v in lv.items() if "Low" in k and bars.iloc[sw].Low < v]}
                break
    return None


def simulate(bars, setup, target, side):
    entry = setup["entry"]
    stop = setup["extreme"] + TICK if side == -1 else setup["extreme"] - TICK
    risk_pts = abs(stop - entry)
    if risk_pts < TICK:
        return None
    contracts = min(MAX_CONTRACTS, math.floor(RISK_USD / (risk_pts * PT_VALUE)))
    if FIXED_CONTRACTS:
        contracts = FIXED_CONTRACTS  # same size every trade; $ risk floats with the stop
    if contracts < 1:
        return {"outcome": "stop_too_wide"}
    exit_px, why, j = None, None, setup["fill_i"]
    first = setup["fill_i"] + (1 if ENTRY_MODE in ("cont", "break") else 0)  # filled on the close → that bar's range is already past
    for j in range(first, len(bars)):
        b = bars.iloc[j]
        hit_stop = b.High >= stop if side == -1 else b.Low <= stop
        hit_tgt = (b.Low <= target if side == -1 else b.High >= target) and j > setup["fill_i"]
        if hit_stop:
            exit_px, why = stop, "stop"
            break
        if hit_tgt:
            exit_px, why = target, "target"
            break
    if exit_px is None:
        exit_px, why = bars.iloc[-1].Close, "time"
    pts = (exit_px - entry) * side
    pnl = pts * PT_VALUE * contracts - FEE_RT * contracts - (SLIP * contracts if why != "target" else 0)
    return {"exit_i": j, "outcome": "win" if pnl > 0 else "loss", "exit": why, "pnl": round(pnl, 2),
            "r": round(pnl / RISK_USD, 2), "risk_pts": round(risk_pts, 2),
            "target_pts": round(abs(target - entry), 2), "contracts": contracts}


def stats(rows):
    if not rows:
        return {"trades": 0}
    pnl = [r["pnl"] for r in rows]
    w = [p for p in pnl if p > 0]
    l = [p for p in pnl if p <= 0]
    eq = peak = dd = 0.0
    streak = worst = 0
    for p in pnl:
        eq += p
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        streak = streak + 1 if p <= 0 else 0
        worst = max(worst, streak)
    return {"trades": len(rows), "win_rate": round(len(w) / len(rows) * 100, 1), "net_pnl": round(sum(pnl), 2),
            "avg_win": round(sum(w) / len(w), 2) if w else 0, "avg_loss": round(sum(l) / len(l), 2) if l else 0,
            "profit_factor": round(sum(w) / -sum(l), 2) if l and sum(l) < 0 else None,
            "avg_r": round(sum(r["r"] for r in rows) / len(rows), 2),
            "max_drawdown": round(dd, 2), "worst_losing_streak": worst}


def monthly(rows):
    out = {}
    for r in rows:
        m = r["date"][:7]
        o = out.setdefault(m, {"trades": 0, "wins": 0, "net": 0.0})
        o["trades"] += 1
        o["wins"] += r["pnl"] > 0
        o["net"] = round(o["net"] + r["pnl"], 2)
    return out


def topstep_50k(rows, target=3000.0, max_loss=2000.0):
    """Back-to-back 50K evaluations: pass at +$3,000, fail when end-of-day balance falls
    $2,000 below its highest end-of-day balance (trailing). Simplified: no consistency
    rule, no daily loss limit."""
    by_day = {}
    for r in rows:
        by_day[r["date"]] = by_day.get(r["date"], 0.0) + r["pnl"]
    passes = fails = 0
    bal = peak = 0.0
    log = []
    start = None
    for day in sorted(by_day):
        start = start or day
        bal += by_day[day]
        peak = max(peak, bal)
        if bal >= target:
            passes += 1
            log.append(f"PASS {start} -> {day}")
            bal = peak = 0.0
            start = None
        elif bal <= peak - max_loss:
            fails += 1
            log.append(f"FAIL {start} -> {day}")
            bal = peak = 0.0
            start = None
    return {"passed": passes, "failed": fails, "in_progress_balance": round(bal, 2), "log": log}


def prepare(df, df15):
    """Everything a day's run needs that is computed once for the whole file."""
    gaps = htf_gaps(df15, "1h") + htf_gaps(df15, "4h")
    return {"df": df, "df15": df15, "gaps": gaps,
            "pdras": gaps + htf_obs(df15, "1h") + htf_obs(df15, "4h"),
            "gaps15": htf_gaps(df15, "15min"),
            "bias_s": htf_bias(df15, BIAS) if BIAS != "none" else None,
            "all_days": sorted({ts.date() for ts in df.index if ts.weekday() < 5})}


def day_levels(ctx, d):
    """The 9:15 prep: Asia/London highs+lows and the fib range. (None, None) if not enough history."""
    df, all_days = ctx["df"], ctx["all_days"]
    d0 = pd.Timestamp(d, tz="America/New_York")
    lv = sessions(df, d0)
    fib = None
    if FIB != "none":
        if d not in all_days or all_days.index(d) < 3:
            return None, None
        k = all_days.index(d)
        H = pd.Timedelta(hours=1)
        if FIB == "pd":
            p0 = pd.Timestamp(all_days[k - 1], tz="America/New_York")
            rng = df[(df.index >= p0 - 6 * H) & (df.index < p0 + 17 * H)]
        elif FIB == "swing":
            p0 = pd.Timestamp(all_days[k - 3], tz="America/New_York")
            rng = df[(df.index >= p0 - 6 * H) & (df.index < d0 + pd.Timedelta(minutes=555))]
        else:
            rng = df[(df.index >= d0 - 6 * H) & (df.index < d0 + pd.Timedelta(minutes=555))]
        if rng.empty:
            return None, None
        fib = (rng.Low.min(), rng.High.max())
    return lv, fib


def run_day(ctx, d, targets=("session", "htf_gap", "pdra", "2R")):
    """Run one trading day. Returns (log row, {target: [trades]}) or None if the day can't be run."""
    df, df15, all_days = ctx["df"], ctx["df15"], ctx["all_days"]
    gaps, pdras, gaps15, bias_s = ctx["gaps"], ctx["pdras"], ctx["gaps15"], ctx["bias_s"]
    d0 = pd.Timestamp(d, tz="America/New_York")
    lv, fib = day_levels(ctx, d)
    if FIB != "none" and fib is None:
        return None
    bars = df[(df.index >= d0 + pd.Timedelta(hours=9)) & (df.index < d0 + pd.Timedelta(hours=int(FLAT_AT[:2]), minutes=int(FLAT_AT[3:])))]
    if lv is None or len(bars) < 10:
        return None
    rows = {name: [] for name in targets}
    entry_log = {"date": str(d), "setup": False, "levels": lv, "fib": fib}
    for name in rows:
        off, notes = 0, []
        for _ in range(MAX_TRADES):
            sub = bars.iloc[off:]
            if len(sub) < 5:
                break
            bias = 0
            if bias_s is not None:
                prior = bias_s[bias_s.index <= sub.index[0]]
                bias = int(prior.iloc[-1]) if len(prior) else 0
            setup = find_setup(sub, lv, bias, fib)
            if not setup:
                break
            entry_log["setup"] = True
            side, entry = setup["side"], setup["entry"]
            stop = setup["extreme"] + TICK if side == -1 else setup["extreme"] - TICK
            at = sub.index[setup["fill_i"]]
            if name == "session":
                opp = [v for k, v in lv.items() if (("Low" in k and v < entry) if side == -1 else ("High" in k and v > entry))]
                tgt = min(opp, key=lambda v: abs(v - entry)) if opp else None
            elif name == "pdra":
                cands = [x for x in (unfilled_gap_target(pdras, df15, at, side, entry),
                                     unfilled_gap_target(gaps15, df15, at, side, entry, max_days=2)) if x is not None]
                tgt = min(cands, key=lambda v: abs(v - entry)) if cands else None
            elif name == "htf_gap":
                tgt = unfilled_gap_target(gaps, df15, at, side, entry)
            else:
                tgt = entry + side * 2 * abs(stop - entry)
            if tgt is None:
                notes.append("no target")
                break
            res = simulate(sub, setup, tgt, side)
            if not res or res.get("outcome") not in ("win", "loss"):
                notes.append((res or {}).get("outcome", "skip"))
                break
            rows[name].append({**res, "date": str(d), "side": side, "entry": round(entry, 2), "stop": round(stop, 2),
                               "target": round(tgt, 2), "fill_t": str(at), "exit_t": str(sub.index[res["exit_i"]]),
                               "sweep_t": setup["sweep_t"], "swept": setup["swept"]})
            notes.append(f'{"S" if side == -1 else "L"} {at.strftime("%H:%M")} {res["exit"]} {res["pnl"]:+.0f}')
            off += res["exit_i"] + 1
        entry_log[name] = " | ".join(notes)
    return entry_log, rows


def main():
    argv = sys.argv[1:]
    if "--days" in argv:
        k = argv.index("--days")
        argv = argv[:k] + argv[k + 2:]
    args = [a for a in argv if not a.startswith("--")]
    days_limit = None
    if "--days" in sys.argv:
        days_limit = int(sys.argv[sys.argv.index("--days") + 1])
    path = args[0]
    df = load(path)
    df15 = load(args[1] if len(args) > 1 else "MNQ_15m.csv")
    days = sorted({ts.date() for ts in df.index if ts.weekday() < 5})
    if days_limit:
        days = days[-days_limit:]

    ctx = prepare(df, df15)
    rows = {"session": [], "htf_gap": [], "pdra": [], "2R": []}
    log = []
    for d in days:
        r = run_day(ctx, d)
        if r is None:
            continue
        entry_log, day_rows = r
        for name in rows:
            rows[name] += day_rows[name]
        log.append({k: v for k, v in entry_log.items() if k not in ("levels", "fib")})

    out = {"file": os.path.basename(path), "sweep_start": SWEEP_START, "max_trades": MAX_TRADES, "entry_mode": ENTRY_MODE, "bias": BIAS, "entry_end": ENTRY_END, "flat_at": FLAT_AT, "inv_max": INV_MAX, "fib": FIB, "close_pts": CLOSE_PTS, "risk_usd": RISK_USD, "days_tested": len(days), "setups": sum(1 for x in log if x["setup"]),
           "by_target": {k: stats(v) for k, v in rows.items()},
           "monthly_session_target": monthly(rows["session"]),
           "topstep_50k_session_target": topstep_50k(rows["session"]),
           "topstep_50k_by_target": {k: {x: y for x, y in topstep_50k(v).items() if x != "log"} for k, v in rows.items()},
           "monthly_pdra_target": monthly(rows["pdra"])}
    tag = os.path.basename(path).replace(".csv", "") + (f"_last{days_limit}" if days_limit else "") + ("" if SWEEP_START == "09:00" else "_from" + SWEEP_START.replace(":", "")) + ("" if MAX_TRADES == 1 else f"_max{MAX_TRADES}") + ("" if ENTRY_MODE == "mid" else f"_{ENTRY_MODE}") + ("" if BIAS == "none" else f"_bias{BIAS}") + ("" if FIB == "none" else f"_fib{FIB}") + ("" if ENTRY_END == "15:00" else f"_end{ENTRY_END.replace(':', '')}") + ("" if FLAT_AT == "16:00" else f"_flat{FLAT_AT.replace(':', '')}") + ("" if not INV_MAX else f"_inv{INV_MAX}") + ("" if RISK_USD == 200 else f"_r{int(RISK_USD)}") + ("" if not FIXED_CONTRACTS else f"_fixed{FIXED_CONTRACTS}")
    pd.DataFrame(log).to_csv(f"sweep_{tag}.csv", index=False)
    with open(f"sweep_{tag}.json", "w") as f:
        json.dump(out, f, indent=2, default=float)
    print(json.dumps(out, indent=2, default=float))


if __name__ == "__main__":
    main()
