#!/usr/bin/env python3
"""Daily VDHG entry monitor: fetch -> indicators -> signal -> CSV -> (maybe) alert.

Usage:
    python analyse_vdhg.py            # normal run
    python analyse_vdhg.py --dry-run  # print the signal; no CSV/state writes, no alert
"""
import argparse
import logging
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import notify
from fetch_data import COLS, DataFetchError, fetch_ohlcv

BASE = Path(__file__).parent
CSV_PATH = BASE / "data" / "vdhg_daily.csv"
LOG_PATH = BASE / "logs" / "error.log"

CSV_COLUMNS = ["date", "open", "high", "low", "close", "volume", "rsi_14", "macd",
               "macd_signal", "macd_histogram", "ema_20", "sma_50", "sma_100", "sma_200",
               "atr_14", "recent_swing_high", "drawdown_from_high", "signal",
               "entry_zone", "reason"]

SWING_WINDOW = 60   # trading days for swing high/low
PIVOT_K = 3         # bars each side to confirm a pivot
TRANCHES = {"SMALL BUY": "~25% of your planned position",
            "ADD": "~25-33% of the remaining planned position"}

log = logging.getLogger("vdhg")


# ---------------------------------------------------------------- indicators
def rsi_wilder(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain, loss = d.clip(lower=0), (-d).clip(lower=0)
    ag = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()   # Wilder smoothing
    al = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = ag / al.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.where(~((al == 0) & (ag > 0)), 100.0)


def atr_wilder(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c = df["close"]
    df["rsi_14"] = rsi_wilder(c)
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_histogram"] = df["macd"] - df["macd_signal"]
    df["ema_20"] = c.ewm(span=20, adjust=False).mean()
    for n in (50, 100, 200):
        df[f"sma_{n}"] = c.rolling(n).mean()
    df["atr_14"] = atr_wilder(df)
    return df


# -------------------------------------------------------------------- levels
def swing_levels(window: pd.DataFrame, close: float, atr: float) -> dict:
    """Swing high/low over the window plus nearest pivot support/resistance."""
    hi, lo = window["high"].to_numpy(), window["low"].to_numpy()
    k, n = PIVOT_K, len(window)
    piv_lows, piv_highs = [], []
    for j in range(k, n - k):
        if lo[j] == lo[j - k:j + k + 1].min():
            piv_lows.append(lo[j])
        if hi[j] == hi[j - k:j + k + 1].max():
            piv_highs.append(hi[j])
    swing_high, swing_low = hi.max(), lo.min()
    supports = [x for x in piv_lows + [swing_low] if x <= close]
    resists = [x for x in piv_highs + [swing_high] if x >= close]
    support = max(supports) if supports else close - 1.5 * atr       # ATR-based fallback
    resistance = min(resists) if resists else close + 1.5 * atr
    prior_low = lo[:-1].min() if n > 1 else swing_low
    return {"swing_high": swing_high, "swing_low": swing_low, "support": support,
            "resistance": resistance, "breakdown": close < prior_low - 0.25 * atr}


def entry_zone(m: dict, buy: bool):
    """Dynamic zone from swing low/high, EMA20, SMA50, ATR support, support, price."""
    c, atr = m["close"], m["atr"]
    if buy:  # price is already in the pullback: accumulate between support/ATR floor and just above price
        high = min(c + 0.25 * atr, m["ema20"]) if m["ema20"] >= c else c
        low = max(m["support"], c - 1.0 * atr)
    else:    # where would a pullback become interesting? nearest anchor below price
        rng = m["swing_high"] - m["swing_low"]
        anchors = [m["ema20"], m["sma50"], c - 1.5 * atr, m["swing_low"] + 0.382 * rng]
        below = [a for a in anchors if a == a and a < c]
        high = max(below) if below else c - 0.5 * atr
        low = max(m["support"], high - 1.0 * atr) if m["support"] < high else high - 1.0 * atr
    low, high = min(low, high), max(low, high)
    return low, high


# -------------------------------------------------------------------- signal
def classify(m: dict):
    """Pure function: metrics dict -> (signal, reason). Precedence:
    AVOID > ADD > SMALL BUY > WATCH > WAIT (WAIT is the default)."""
    c, atr, rsi, hist = m["close"], m["atr"], m["rsi"], m["hist"]
    hist_up = hist >= m["hist1"] - 0.05 * atr            # stopped falling / turning up
    hist_up2 = hist > m["hist1"] > m["hist2"]            # improving two sessions
    hist_strong = hist - m["hist1"] > 0.10 * atr and m["hist1"] > m["hist2"]
    rsi_falling = rsi < m["rsi3"]
    macd_down = m["macd"] < 0 and m["macd"] < m["macd1"] and hist < m["hist1"]
    below50, below200 = c < m["sma50"], c < m["sma200"]  # False when SMA is NaN
    dd = m["drawdown"]

    if (below50 and below200 and (rsi_falling or macd_down)) or m["breakdown"]:
        why = "clear breakdown below recent support" if m["breakdown"] else \
            "below 50 & 200-day SMA with " + ("falling RSI" if rsi_falling else "MACD expanding lower")
        return "AVOID", why

    near_support = (c - m["support"]) <= 1.5 * atr
    if rsi <= 35 and near_support and hist_up2:
        return "ADD", (f"RSI {rsi:.0f} oversold, price within {(c - m['support']) / atr:.1f} ATR of "
                       f"support ${m['support']:.2f}, MACD histogram improving 2+ sessions")

    lower_pullback = m["position"] <= 0.40 and c <= m["ema20"]
    if 35 < rsi <= 40 and lower_pullback and hist_up and not hist_strong and c > m["sma200"]:
        return "SMALL BUY", (f"RSI {rsi:.0f}, price in lower {m['position'] * 100:.0f}% of 60-day range "
                             f"({dd:.1f}% off high), MACD histogram stabilising, above 200-day SMA")

    flat = abs(hist - m["hist1"]) <= 0.05 * atr or -0.3 * atr <= hist < 0
    if -4 <= dd <= -2 and 40 <= rsi <= 48 and flat:
        return "WATCH", f"{dd:.1f}% below swing high, RSI {rsi:.0f}, MACD flattening"

    parts = []
    if rsi > 45: parts.append(f"RSI {rsi:.0f} > 45")
    if c > m["ema20"]: parts.append("price above 20-day EMA")
    if hist_strong: parts.append("MACD histogram rising strongly")
    return "WAIT", "; ".join(parts) or "no pullback setup"


def analyse(df: pd.DataFrame) -> pd.DataFrame:
    """Add indicators and per-row signal columns (extra columns are kept in memory only)."""
    df = add_indicators(df)
    out = {k: [np.nan] * len(df) for k in
           ("recent_swing_high", "drawdown_from_high", "support", "resistance", "inval")}
    sig, zone, reason = ["N/A"] * len(df), [""] * len(df), [""] * len(df)
    need = ["rsi_14", "atr_14", "macd"]
    for i in range(len(df)):
        row = df.iloc[i]
        if i < 20 or row[need].isna().any():
            continue
        w = df.iloc[max(0, i - SWING_WINDOW + 1): i + 1]
        lv = swing_levels(w, row["close"], row["atr_14"])
        rng = lv["swing_high"] - lv["swing_low"]
        p = lambda col, k: df[col].iloc[i - k] if i - k >= 0 else np.nan
        m = dict(close=row["close"], atr=row["atr_14"], rsi=row["rsi_14"], rsi3=p("rsi_14", 3),
                 macd=row["macd"], macd1=p("macd", 1), hist=row["macd_histogram"],
                 hist1=p("macd_histogram", 1), hist2=p("macd_histogram", 2),
                 ema20=row["ema_20"], sma50=row["sma_50"], sma200=row["sma_200"],
                 swing_high=lv["swing_high"], swing_low=lv["swing_low"],
                 support=lv["support"], resistance=lv["resistance"], breakdown=lv["breakdown"],
                 drawdown=(row["close"] / lv["swing_high"] - 1) * 100,
                 position=(row["close"] - lv["swing_low"]) / rng if rng > 0 else 0.5)
        if any(pd.isna(m[k]) for k in ("rsi3", "macd1", "hist1", "hist2")):
            continue
        s, why = classify(m)
        lo_z, hi_z = entry_zone(m, s in notify.ALERT_SIGNALS)
        sig[i], zone[i], reason[i] = s, f"{lo_z:.2f}-{hi_z:.2f}", why
        out["recent_swing_high"][i], out["drawdown_from_high"][i] = lv["swing_high"], m["drawdown"]
        out["support"][i], out["resistance"][i] = lv["support"], lv["resistance"]
        out["inval"][i] = lv["support"] - 0.5 * row["atr_14"]
    for k, v in out.items():
        df[k] = v
    df["signal"], df["entry_zone"], df["reason"] = sig, zone, reason
    return df


def latest_summary(df: pd.DataFrame) -> dict:
    r = df.iloc[-1]
    s = r["signal"]
    return {"date": df.index[-1].strftime("%Y-%m-%d"), "close": r["close"], "signal": s,
            "entry_zone": r["entry_zone"], "tranche": TRANCHES.get(s, "n/a"),
            "rsi_14": r["rsi_14"], "macd": r["macd"], "macd_signal": r["macd_signal"],
            "macd_histogram": r["macd_histogram"], "ema_20": r["ema_20"],
            "sma_50": r["sma_50"], "sma_200": r["sma_200"], "support": r["support"],
            "resistance": r["resistance"], "recent_swing_high": r["recent_swing_high"],
            "drawdown_from_high": r["drawdown_from_high"], "reason": r["reason"],
            "invalidation": r["inval"]}


# ----------------------------------------------------------------------- CSV
def merge_with_existing(new: pd.DataFrame, path: Path = None) -> pd.DataFrame:
    """Keep older rows already in the CSV; fresh data wins on overlapping dates."""
    path = path or CSV_PATH
    if path.exists():
        old = pd.read_csv(path, index_col="date", parse_dates=True)[COLS]
        new = pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
    return new


def write_csv(df: pd.DataFrame, path: Path = None) -> None:
    path = path or CSV_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out.index = out.index.strftime("%Y-%m-%d")
    out.index.name = "date"
    num = out.select_dtypes("number").columns
    out[num] = out[num].round(4)
    out.reset_index()[CSV_COLUMNS].to_csv(path, index=False)


# ---------------------------------------------------------------------- main
def setup_logging():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_PATH)
    fh.setLevel(logging.WARNING)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(fh)
    log.addHandler(sh)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Daily VDHG entry monitor")
    ap.add_argument("--dry-run", action="store_true",
                    help="calculate and print the signal; write nothing, send nothing")
    ap.add_argument("--ticker", default="VDHG.AX")
    ap.add_argument("--period", default="2y", help="history to fetch (yfinance period)")
    args = ap.parse_args(argv)
    setup_logging()

    try:
        fresh = fetch_ohlcv(args.ticker, args.period)
    except DataFetchError as e:
        log.error("Data fetch failed, no alert sent: %s", e)
        return 1
    except Exception:  # noqa: BLE001
        log.exception("Unexpected error while fetching data")
        return 1

    try:
        df = analyse(merge_with_existing(fresh))
        res = latest_summary(df)
        if not args.dry_run:
            write_csv(df)
    except Exception:  # noqa: BLE001
        log.exception("Analysis failed, no alert sent")
        return 1

    print(f"{res['date']}  {args.ticker}  close ${res['close']:.2f}  signal: {res['signal']}")
    print(f"  RSI {res['rsi_14']:.1f}  MACD {res['macd']:.3f}  hist {res['macd_histogram']:.3f}"
          f"  | entry zone {res['entry_zone']}  support {res['support']:.2f}"
          f"  resistance {res['resistance']:.2f}")
    print(f"  reason: {res['reason']}")

    if res["signal"] not in notify.ALERT_SIGNALS:
        return 0
    cooldown = int(os.environ.get("ALERT_COOLDOWN_TRADING_DAYS", "0"))
    send, why = notify.should_alert(res["date"], res["signal"], notify.load_state(), cooldown)
    if args.dry_run:
        print(f"  [dry-run] would {'SEND' if send else 'SUPPRESS'} alert: {why}")
        return 0
    if not send:
        print(f"  alert suppressed: {why}")
        return 0
    try:
        notify.send_notification(res)
        notify.save_state(res["date"], res["signal"])
        print(f"  alert sent ({why})")
    except Exception:  # noqa: BLE001
        log.exception("Notification failed")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
