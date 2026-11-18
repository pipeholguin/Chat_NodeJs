# VDHG Daily Entry Monitor

Fetches daily OHLCV for `VDHG.AX`, computes indicators, classifies the setup
(WAIT / WATCH / SMALL BUY / ADD / AVOID), keeps a persistent CSV and sends an alert
**only** for SMALL BUY or ADD. Informational only, not financial advice.

## Setup
```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python analyse_vdhg.py --dry-run   # calculate + print, write/send nothing
python analyse_vdhg.py             # update data/vdhg_daily.csv, alert if needed
pytest                             # tests (synthetic data, no network)
```

Data: yfinance first, Stooq CSV fallback (raw, unadjusted close). If both fail (or the
latest bar is >7 days old) nothing is alerted, the error goes to `logs/error.log`, exit code 1.
Exit code 2 = notification delivery failed (state is not updated, so the next run retries).

## Notifications (env vars, or a `.env` file read by `run_daily.sh`)
```
NOTIFICATION_METHOD=webhook   WEBHOOK_URL=https://...        # Slack/Discord/Teams-style JSON
NOTIFICATION_METHOD=email     SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD EMAIL_TO [EMAIL_FROM]
ALERT_COOLDOWN_TRADING_DAYS=0   # optional, see below
```
Port 465 uses SSL, other ports use STARTTLS.

**Duplicate suppression** (`state/last_alert.json` = `{date, signal}`): alert if nothing was
alerted before, if the date changed, or if the signal went SMALL BUY -> ADD on the same date.
Because "date changed" already covers the "3 trading days" rule, a persisting signal alerts every
new day by default. Set `ALERT_COOLDOWN_TRADING_DAYS=3` to require 3 trading days between alerts
(an SMALL BUY -> ADD escalation on the same day still alerts).

## Scheduling
Run after the ASX close so the bar is final (cron, Sydney time):
`30 16 * * 1-5 /path/to/vdhg-monitor/run_daily.sh >> /path/to/vdhg-monitor/logs/cron.log 2>&1`

## How signals are decided
Window: 60 trading days. Swing high/low = highest high / lowest low; support/resistance = nearest
confirmed pivot low/high (3 bars each side) or the swing low/high, ATR-based if none.
Drawdown = close vs swing high. Position = where close sits in the swing low-high range.
Precedence: **AVOID > ADD > SMALL BUY > WATCH > WAIT** (WAIT is the default).

| Signal | Rule as implemented |
|---|---|
| AVOID | close below SMA50 and SMA200 **and** (RSI below its value 3 days ago, or MACD<0 with MACD and histogram falling); **or** close < prior 60d low - 0.25 ATR (breakdown) |
| ADD | RSI <= 35 (no lower bound), close within 1.5 ATR of support, histogram up two sessions in a row |
| SMALL BUY | 35 < RSI <= 40, close in lower 40% of range and <= EMA20, histogram not falling (>= prior - 0.05 ATR) and not rising strongly, close > SMA200 |
| WATCH | drawdown -2% to -4%, RSI 40-48, histogram flat (±0.05 ATR move) or slightly negative (> -0.3 ATR) |
| WAIT | otherwise (reason lists: RSI > 45, price > EMA20, histogram rising strongly) |

"Rising strongly" = histogram up > 0.1 ATR today and up the day before. These thresholds are
judgement calls: tune the constants in `classify()`.

**Entry zone** (no fixed prices): for buy signals, from `max(support, close - 1 ATR)` up to
`min(close + 0.25 ATR, EMA20)`. Otherwise, the nearest anchor below price among EMA20, SMA50,
close - 1.5 ATR and the 38.2% level of the swing range, down 1 ATR (floored at support).
**Invalidation** = support - 0.5 ATR (daily close below it). **Tranche**: SMALL BUY ~25% of the
planned position, ADD ~25-33% of the remainder (edit `TRANCHES`).

## Files
`fetch_data.py` data sources - `analyse_vdhg.py` indicators/signals/CSV/main -
`notify.py` message, delivery, dedupe - `run_daily.sh` cron wrapper - `tests/`.
CSV columns: date, open, high, low, close, volume, rsi_14, macd, macd_signal, macd_histogram,
ema_20, sma_50, sma_100, sma_200, atr_14, recent_swing_high, drawdown_from_high, signal,
entry_zone, reason. Existing rows are kept; indicators are recomputed over the merged history
each run (first ~200 rows have blank SMA200).
