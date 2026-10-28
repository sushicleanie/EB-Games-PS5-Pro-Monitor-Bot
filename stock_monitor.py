#!/usr/bin/env python3
"""PS5 Pro stock monitor for Australian retailers.

Rules this script follows:
- It never tries to get around blocks (no spoofing, no CAPTCHA tricks).
- 403/429, CAPTCHA pages, or robots.txt restrictions => status BLOCKED.
- Anything unclear => status UNKNOWN. Never treated as in stock or out of stock.
- It only alerts "in stock" when the page's own structured data (schema.org
  JSON-LD) says InStock for a new PS5 Pro console.
- Secrets come only from environment variables.
"""
import html
import json
import os
import re
import sys
import time
from urllib import robotparser
from urllib.parse import urlparse

import requests

CONFIG_FILE = "retailers.json"
STATE_FILE = "state.json"
USER_AGENT = "PS5ProStockMonitor/1.0 (personal low-frequency hobby monitor)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
TIMEOUT = 25
UNKNOWN_ALERT_AFTER = 8  # consecutive unclear runs (~2 hours) before one heads-up

IN_STOCK = "IN_STOCK"
OUT_OF_STOCK = "OUT_OF_STOCK"
BLOCKED = "BLOCKED"
UNKNOWN = "UNKNOWN"
NOT_CONFIGURED = "NOT_CONFIGURED"

NAME_RE = re.compile(r"(ps5|playstation\s*5)\s*pro", re.I)
ACCESSORY_RE = re.compile(
    r"controller|cover|faceplate|skin|case|charger|charging|headset|stand|"
    r"dock|cable|cooling|remote|camera|gift card|subscription",
    re.I,
)
BLOCK_MARKERS = [
    "enter the characters you see below",
    "robot check",
    "unusual traffic",
    "access denied",
    "verify you are a human",
    "are you a robot",
    "captcha",
]
LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)


def log(msg):
    print(msg, flush=True)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret is missing/empty.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            return True
        log(f"ERROR: Telegram rejected the message (HTTP {r.status_code}). "
            "Check the chat ID, and that you pressed Start on your bot.")
        return False
    except requests.RequestException as e:
        log(f"ERROR: Could not reach Telegram ({type(e).__name__}).")
        return False


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def robots_allows(url):
    p = urlparse(url)
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt",
                         headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException:
        return True
    if r.status_code != 200:
        return True
    rp = robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp.can_fetch(USER_AGENT, url)


def iter_nodes(obj):
    if isinstance(obj, list):
        for item in obj:
            yield from iter_nodes(item)
    elif isinstance(obj, dict):
        yield obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from iter_nodes(v)


def is_product(node):
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return "Product" in types


def classify_availability(value):
    a = str(value).rsplit("/", 1)[-1].lower()
    if a in ("instock", "onlineonly", "limitedavailability"):
        return IN_STOCK
    if a in ("outofstock", "soldout", "discontinued"):
        return OUT_OF_STOCK
    return UNKNOWN


def parse_product(page_html):
    """Return {'status','detail','price'} or None if no matching product data."""
    for block in LD_RE.findall(page_html):
        try:
            data = json.loads(html.unescape(block.strip()))
        except json.JSONDecodeError:
            continue
        for node in iter_nodes(data):
            if not is_product(node):
                continue
            name = html.unescape(str(node.get("name", "")))
            if not NAME_RE.search(name):
                continue
            if ACCESSORY_RE.search(name):
                return {"status": UNKNOWN,
                        "detail": f"Page looks like an accessory: {name[:60]}"}
            statuses, price = [], None
            for off in iter_nodes(node.get("offers", [])):
                if "availability" not in off:
                    continue
                cond = str(off.get("itemCondition", ""))
                if cond and "new" not in cond.lower():
                    continue  # skip used/refurbished offers
                st = classify_availability(off["availability"])
                statuses.append(st)
                if st == IN_STOCK and price is None:
                    amount = off.get("price") or off.get("lowPrice")
                    if amount:
                        price = f"{off.get('priceCurrency', 'AUD')} {amount}"
            if IN_STOCK in statuses:
                return {"status": IN_STOCK, "detail": name[:80], "price": price}
            if statuses and all(s == OUT_OF_STOCK for s in statuses):
                return {"status": OUT_OF_STOCK, "detail": name[:80]}
            return {"status": UNKNOWN,
                    "detail": "Product found but availability unclear"}
    return None


def check_retailer(r):
    url = str(r.get("url", "")).strip()
    if not url.startswith("http"):
        return {"status": NOT_CONFIGURED, "detail": "No URL set in retailers.json"}
    if not robots_allows(url):
        return {"status": BLOCKED, "detail": "robots.txt disallows automated access"}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
        return {"status": UNKNOWN, "detail": f"Network error: {type(e).__name__}"}
    code = resp.status_code
    if code in (401, 403, 429):
        return {"status": BLOCKED, "detail": f"HTTP {code}"}
    low = resp.text.lower()
    if code == 200:
        result = parse_product(resp.text)
        if result:
            return result
    if any(m in low for m in BLOCK_MARKERS):
        return {"status": BLOCKED, "detail": f"HTTP {code}, bot-check page"}
    if code != 200:
        return {"status": UNKNOWN, "detail": f"HTTP {code}"}
    return {"status": UNKNOWN, "detail": "No machine-readable stock info on page"}


def main():
    if os.environ.get("SEND_TEST", "").lower() == "true":
        ok = send_telegram("✅ Test message from your PS5 Pro monitor. Telegram is working.")
        log("Test message sent." if ok else "Test message FAILED.")
        return 0 if ok else 1

    retailers = load_json(CONFIG_FILE, [])
    if not retailers:
        log(f"ERROR: {CONFIG_FILE} is missing or empty.")
        return 1

    state = load_json(STATE_FILE, {})
    failures = 0
    log(f"{'Retailer':<14} {'Status':<15} Detail")

    for i, r in enumerate(retailers):
        if i:
            time.sleep(3)
        name = r["name"]
        url = r.get("url", "")
        res = check_retailer(r)
        status = res["status"]
        log(f"{name:<14} {status:<15} {res.get('detail', '')}")

        s = state.get(name, {})
        s.setdefault("alerted", False)
        s.setdefault("blocked_notified", False)
        s.setdefault("unknown_streak", 0)

        if status == IN_STOCK:
            s["unknown_streak"] = 0
            s["blocked_notified"] = False
            if not s["alerted"]:
                msg = f"🟢 PS5 Pro IN STOCK at {name}\n"
                if res.get("price"):
                    msg += f"Price: {res['price']}\n"
                msg += url
                if send_telegram(msg):
                    s["alerted"] = True
                else:
                    failures += 1  # will retry next run
        elif status == OUT_OF_STOCK:
            s["alerted"] = False
            s["blocked_notified"] = False
            s["unknown_streak"] = 0
        elif status == BLOCKED:
            if not s["blocked_notified"]:
                msg = (f"⚠️ {name} is blocking automated checks ({res.get('detail')}).\n"
                       "I am NOT treating this as in or out of stock, and I won't "
                       "try to bypass it. Check this retailer manually.")
                if send_telegram(msg):
                    s["blocked_notified"] = True
                else:
                    failures += 1
        elif status == UNKNOWN:
            s["unknown_streak"] += 1
            if s["unknown_streak"] == UNKNOWN_ALERT_AFTER:
                send_telegram(f"ℹ️ {name}: stock status has been unclear for "
                              f"{UNKNOWN_ALERT_AFTER} checks in a row "
                              f"({res.get('detail')}). Not treated as in or out of stock.")
        s["status"] = status
        state[name] = s

    save_state(state)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
