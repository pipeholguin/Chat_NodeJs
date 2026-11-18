"""Notifications (webhook or email) and duplicate-alert suppression."""
import json
import os
import smtplib
import urllib.request
from email.message import EmailMessage
from pathlib import Path

import numpy as np
import pandas as pd

ALERT_SIGNALS = ("SMALL BUY", "ADD")
STATE_FILE = Path(__file__).parent / "state" / "last_alert.json"


# ---------- duplicate prevention ----------
def load_state(path: Path = None) -> dict:
    try:
        return json.loads(Path(path or STATE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(date: str, signal: str, path: Path = None) -> None:
    path = Path(path or STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": date, "signal": signal}, indent=2))


def trading_days_between(d1: str, d2: str) -> int:
    return int(np.busday_count(pd.Timestamp(d1).date(), pd.Timestamp(d2).date()))


def should_alert(date: str, signal: str, state: dict, cooldown_days: int = 0):
    """Return (bool, reason). Rules:
    - nothing alerted before            -> send
    - same date, SMALL BUY -> ADD       -> send (escalation)
    - same date otherwise               -> suppress
    - new date and >= cooldown_days trading days since last alert -> send
      (cooldown_days=0 means any new date qualifies, as specified)
    """
    if signal not in ALERT_SIGNALS:
        return False, f"signal {signal} never alerts"
    if not state:
        return True, "first alert"
    last_date, last_signal = state.get("date"), state.get("signal")
    if date == last_date:
        if last_signal == "SMALL BUY" and signal == "ADD":
            return True, "escalated SMALL BUY -> ADD"
        return False, "already alerted today"
    gap = trading_days_between(last_date, date)
    if gap >= cooldown_days:
        return True, f"new date ({gap} trading days since last alert)"
    return False, f"only {gap} trading days since last alert (< {cooldown_days})"


# ---------- message ----------
def build_message(r: dict):
    """r is the result dict from analyse_vdhg.latest_summary()."""
    subject = f"VDHG {r['signal']} - {r['date']} @ ${r['close']:.2f}"
    rel = lambda ma: f"{(r['close'] / ma - 1) * 100:+.1f}%" if ma == ma else "n/a"
    body = "\n".join([
        f"Date: {r['date']}    Price: ${r['close']:.2f}",
        f"Signal: {r['signal']}",
        f"Entry zone: {r['entry_zone']}",
        f"Suggested tranche: {r['tranche']}",
        "",
        f"RSI(14): {r['rsi_14']:.1f}   MACD: {r['macd']:.3f}   "
        f"Signal: {r['macd_signal']:.3f}   Histogram: {r['macd_histogram']:.3f}",
        f"Price vs EMA20: {rel(r['ema_20'])} (${r['ema_20']:.2f})   "
        f"SMA50: {rel(r['sma_50'])}   SMA200: {rel(r['sma_200'])}",
        f"Support: ${r['support']:.2f}   Resistance: ${r['resistance']:.2f}   "
        f"Swing high (60d): ${r['recent_swing_high']:.2f} ({r['drawdown_from_high']:.1f}%)",
        "",
        f"Reason: {r['reason']}",
        f"Invalidation: daily close below ${r['invalidation']:.2f}",
        "",
        "Informational only - not financial advice.",
    ])
    return subject, body


# ---------- delivery ----------
def _send_webhook(subject: str, body: str, payload: dict) -> None:
    url = os.environ.get("WEBHOOK_URL")
    if not url:
        raise RuntimeError("WEBHOOK_URL is not set")
    text = f"*{subject}*\n{body}"
    # 'text' (Slack/Teams-style) and 'content' (Discord) both included.
    data = json.dumps({"text": text, "content": text[:1990], "data": payload}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"webhook returned HTTP {resp.status}")


def _send_email(subject: str, body: str) -> None:
    need = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing env vars for email: {', '.join(missing)}")
    host, port = os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, os.environ["EMAIL_TO"]
    msg["From"] = os.environ.get("EMAIL_FROM", os.environ["SMTP_USER"])
    msg.set_content(body)
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    with server:
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)


def send_notification(result: dict) -> None:
    subject, body = build_message(result)
    method = os.environ.get("NOTIFICATION_METHOD", "").lower()
    if method == "webhook":
        _send_webhook(subject, body, {k: (None if v != v else v) for k, v in result.items()
                                      if isinstance(v, (str, int, float))})
    elif method == "email":
        _send_email(subject, body)
    else:
        raise RuntimeError("NOTIFICATION_METHOD must be 'webhook' or 'email'")
