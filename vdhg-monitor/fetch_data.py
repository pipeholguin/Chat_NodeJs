"""Fetch daily OHLCV data for a ticker. yfinance first, Stooq CSV as fallback."""
import io
import logging
import urllib.request
from datetime import datetime, timedelta

import pandas as pd

log = logging.getLogger("vdhg")

COLS = ["open", "high", "low", "close", "volume"]
MAX_STALENESS_DAYS = 7  # last bar older than this => treat as a failed fetch


class DataFetchError(Exception):
    pass


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):  # newer yfinance returns (field, ticker)
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    df = df[COLS].dropna(subset=["open", "high", "low", "close"])
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "date"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df["volume"] = df["volume"].fillna(0)
    return df


def _from_yfinance(ticker: str, period: str) -> pd.DataFrame:
    import yfinance as yf

    # auto_adjust=False keeps the raw close that brokers show (ETF distributions
    # would otherwise shift historical prices).
    df = yf.download(ticker, period=period, interval="1d", auto_adjust=False,
                     progress=False, threads=False)
    if df is None or df.empty:
        raise DataFetchError("yfinance returned no data")
    return _clean(df)


def _from_stooq(ticker: str) -> pd.DataFrame:
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}&i=d"
    with urllib.request.urlopen(url, timeout=20) as r:
        text = r.read().decode("utf-8", "replace")
    if not text.lower().startswith("date"):
        raise DataFetchError(f"stooq returned unexpected content: {text[:80]!r}")
    df = pd.read_csv(io.StringIO(text), index_col="Date", parse_dates=True)
    if df.empty:
        raise DataFetchError("stooq returned no rows")
    return _clean(df)


def fetch_ohlcv(ticker: str = "VDHG.AX", period: str = "2y") -> pd.DataFrame:
    """Return a DataFrame indexed by date with open/high/low/close/volume."""
    errors = []
    for name, fn in (("yfinance", lambda: _from_yfinance(ticker, period)),
                     ("stooq", lambda: _from_stooq(ticker))):
        try:
            df = fn()
            if df.empty:
                raise DataFetchError("empty after cleaning")
            age = datetime.now() - df.index[-1].to_pydatetime()
            if age > timedelta(days=MAX_STALENESS_DAYS):
                raise DataFetchError(f"latest bar {df.index[-1].date()} is stale")
            log.info("Fetched %d rows from %s (last bar %s)", len(df), name, df.index[-1].date())
            return df
        except Exception as e:  # noqa: BLE001 - try the next source
            errors.append(f"{name}: {e}")
            log.warning("Source %s failed: %s", name, e)
    raise DataFetchError("; ".join(errors))
