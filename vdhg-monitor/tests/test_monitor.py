import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import analyse_vdhg as a
import notify


def synthetic(n=320, seed=1, shape="pullback"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    base = 60 + np.cumsum(rng.normal(0.05, 0.25, n))
    if shape == "pullback":
        base[-25:] -= np.linspace(0, 4, 25)
    close = pd.Series(base, index=idx)
    return pd.DataFrame({"open": close.shift(1).fillna(close), "high": close + 0.2,
                         "low": close - 0.2, "close": close, "volume": 1000}, index=idx)


def base_metrics(**kw):
    m = dict(close=70, atr=0.5, rsi=50, rsi3=50, macd=0.1, macd1=0.1, hist=0.0, hist1=0.0,
             hist2=0.0, ema20=69, sma50=68, sma200=65, swing_high=72, swing_low=66, support=67,
             resistance=72, breakdown=False, drawdown=-2.8, position=0.67)
    m.update(kw)
    return m


def test_rsi_extremes():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert a.rsi_wilder(up).iloc[-1] == 100
    assert a.rsi_wilder(-up).iloc[-1] == pytest.approx(0, abs=1e-6)


def test_indicator_columns_and_nan_warmup():
    df = a.add_indicators(synthetic())
    assert df["sma_200"].iloc[:199].isna().all() and df["sma_200"].iloc[-1] == df["close"].iloc[-200:].mean()
    assert np.allclose(df["macd_histogram"], df["macd"] - df["macd_signal"])
    assert (df["atr_14"].dropna() > 0).all()


def test_classify_rules():
    assert a.classify(base_metrics())[0] == "WAIT"
    assert a.classify(base_metrics(rsi=38, position=0.3, close=68.5, ema20=69.5, hist=0.01,
                                   hist1=0.0, hist2=0.0))[0] == "SMALL BUY"
    assert a.classify(base_metrics(rsi=38, position=0.3, close=64, sma200=65, ema20=69))[0] != "SMALL BUY"
    assert a.classify(base_metrics(rsi=32, close=67.5, position=0.1, ema20=69, hist=0.1,
                                   hist1=0.0, hist2=-0.1))[0] == "ADD"
    assert a.classify(base_metrics(close=60, sma50=65, sma200=66, rsi=30, rsi3=40))[0] == "AVOID"
    assert a.classify(base_metrics(breakdown=True))[0] == "AVOID"
    assert a.classify(base_metrics(rsi=44, drawdown=-3, close=69.8, ema20=70.5))[0] == "WATCH"


def test_analyse_runs_and_signal_values():
    df = a.analyse(synthetic())
    assert set(df["signal"]).issubset({"WAIT", "WATCH", "SMALL BUY", "ADD", "AVOID", "N/A"})
    last = df.iloc[-1]
    lo, hi = map(float, last["entry_zone"].split("-"))
    assert lo <= hi and (df["drawdown_from_high"].dropna() <= 0).all()


def test_csv_roundtrip_and_merge(tmp_path):
    raw = synthetic()
    p = tmp_path / "vdhg.csv"
    a.write_csv(a.analyse(raw), p)
    saved = pd.read_csv(p)
    assert list(saved.columns) == a.CSV_COLUMNS
    merged = a.merge_with_existing(raw.iloc[-50:], p)   # old rows preserved
    assert len(merged) == len(raw)


def test_dedupe_rules():
    s = {"date": "2025-01-06", "signal": "SMALL BUY"}
    assert notify.should_alert("2025-01-06", "SMALL BUY", s)[0] is False
    assert notify.should_alert("2025-01-06", "ADD", s)[0] is True
    assert notify.should_alert("2025-01-07", "SMALL BUY", s)[0] is True
    assert notify.should_alert("2025-01-07", "SMALL BUY", s, cooldown_days=3)[0] is False
    assert notify.should_alert("2025-01-09", "SMALL BUY", s, cooldown_days=3)[0] is True
    assert notify.should_alert("2025-01-07", "WAIT", {})[0] is False
    assert notify.should_alert("2025-01-07", "ADD", {})[0] is True


def test_dry_run_and_fetch_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(a, "CSV_PATH", tmp_path / "d.csv")
    monkeypatch.setattr(a, "LOG_PATH", tmp_path / "error.log")
    monkeypatch.setattr(a, "fetch_ohlcv", lambda *x: synthetic())
    sent = []
    monkeypatch.setattr(notify, "send_notification", lambda r: sent.append(r))
    assert a.main(["--dry-run"]) == 0
    assert not sent and not (tmp_path / "d.csv").exists()

    def boom(*x):
        raise a.DataFetchError("no network")
    monkeypatch.setattr(a, "fetch_ohlcv", boom)
    assert a.main([]) == 1 and not sent
    assert "no network" in (tmp_path / "error.log").read_text()
