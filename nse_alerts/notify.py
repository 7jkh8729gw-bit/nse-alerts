"""Telegram sender: plain HTTPS (no extra library), retries, never raises."""
import html
import logging
import time
import requests
from . import config

log = logging.getLogger("notify")


def esc(s) -> str:
    return html.escape(str(s))


def send(text: str, silent: bool = False) -> bool:
    if not config.BOT_TOKEN or not config.CHAT_ID:
        log.warning("Telegram not configured; message was:\n%s", text)
        return False
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    payload = {"chat_id": config.CHAT_ID, "text": text[:4000], "parse_mode": "HTML",
               "disable_web_page_preview": True, "disable_notification": silent}
    for attempt in range(4):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 2) + 1)
                continue
            log.warning("Telegram HTTP %s: %s", r.status_code, r.text[:200])
        except Exception as e:  # network blips must never kill the monitor
            log.warning("Telegram error: %s", e)
        time.sleep(1.5 * (attempt + 1))
    return False
