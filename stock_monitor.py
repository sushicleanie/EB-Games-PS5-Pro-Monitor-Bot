#!/usr/bin/env python3
"""PS5 Pro stock monitor (loose, any-sign-of-stock version, v2.2).

- Never tries to get around blocks. 401/403/429, bot-check pages, or
  robots.txt restrictions => BLOCKED (reported, never guessed).
- Bot-check wording is only looked for in the visible page text, and only
  when no product data was found.
- Per-retailer rules in retailers.json:
    out_of_stock_text: phrases that mean sold out (visible text wins over
      everything; page code is a fallback when there is no product data).
    in_stock_if_absent: true => in stock only if the page loaded, "PS5 Pro"
      is in the visible text, and the out-of-stock phrase is absent.
- STRONG signal: schema.org JSON-LD says InStock.
- WEAK signal: no JSON-LD stock data, in-stock wording or cart button seen,
  and no sold-out wording.
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
USER_AGENT = "PS5ProStockMonitor/2.2 (personal hobby monitor)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
TIMEOUT = 30
ROBOTS_TIMEOUT = 10
UNKNOWN_ALERT_AFTER = 24  # ~2 hours at 5-minute checks, then one heads-up
RESET_AFTER = 2           # non-in-stock checks in a row before alerts re-arm

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
NEGATIVE_PHRASES = [
    "out of stock",
    "not in stock",
    "sold out",
    "currently unavailable",
    "unavailable online",
    "not available online",
    "no longer available",
    "temporarily out",
    "back in stock soon",
    "notify me",
    "email me when",
    "waitlist",
    "coming soon",
]
POSITIVE_PHRASES = [
    "in stock",
    "available online",
    "available for delivery",
    "available now",
    "add to cart",
    "add to bag",
    "add to basket",
    "add to trolley",
    "buy now",
    "click & collect",
    "click and collect",
]
LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
HIDDEN_RE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1>", re.S | re.I
)
TAG_RE = re.compile(r"<[^>]+>")


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
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        log(f"ERROR: {path} is not valid JSON: {e.msg} at line {e.lineno}, "
            f"column {e.colno}.")
        return default


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def robots_allows(url):
    p = urlparse(url)
    try:
        r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt",
                         headers=HEADERS, timeout=ROBOTS_TIMEOUT)
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


def visible_text(page_html):
    t = HIDDEN_RE.sub(" ", page_html)
    t = TAG_RE.sub(" ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).lower()


def parse_ld(page_html):
    """Return dict(status, raw, price, name) from JSON-LD, or None."""
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
                return {"status": UNKNOWN, "raw": "accessory page",
                        "name": name[:50], "price": None}
            statuses, raws, price = [], [], None
            for off in iter_nodes(node.get("offers", [])):
                if "availability" not in off:
                    continue
                cond = str(off.get("itemCondition", ""))
                if cond and "new" not in cond.lower():
                    continue
                raws.append(str(off["availability"]).rsplit("/", 1)[-1])
                st = classify_availability(off["availability"])
                statuses.append(st)
                if st == IN_STOCK and price is None:
                    amount = off.get("price") or off.get("lowPrice")
                    if amount:
                        price = f"{off.get('priceCurrency', 'AUD')} {amount}"
            raw = ",".join(raws) if raws else "none"
            if IN_STOCK in statuses:
                return {"status": IN_STOCK, "raw": raw,
                        "name": name[:50], "price": price}
            if statuses and all(s == OUT_OF_STOCK for s in statuses):
                return {"status": OUT_OF_STOCK, "raw": raw,
                        "name": name[:50], "price": None}
            return {"status": UNKNOWN, "raw": raw,
                    "name": name[:50], "price": None}
    return None


def get_page(url):
    """One request, with one retry on a network timeout/error."""
    last = None
    for attempt in range(2):
        try:
            return requests.get(url, headers=HEADERS, timeout=TIMEOUT), None
        except requests.RequestException as e:
            last = type(e).__name__
            time.sleep(2)
    return None, last


def check_retailer(r):
    url = str(r.get("url", "")).strip()
    if not url.startswith("http"):
        return {"status": NOT_CONFIGURED, "detail": "No URL set in retailers.json"}
    if not robots_allows(url):
        return {"status": BLOCKED, "detail": "robots.txt disallows automated access"}
    resp, err = get_page(url)
    if resp is None:
        return {"status": UNKNOWN, "detail": f"Network error: {err}"}
    code = resp.status_code
    if code in (401, 403, 429):
        return {"status": BLOCKED, "detail": f"HTTP {code}"}
    if code != 200:
        return {"status": UNKNOWN, "detail": f"HTTP {code}"}

    text = visible_text(resp.text)
    raw_low = resp.text.lower()
    ld = parse_ld(resp.text)

    if ld is None:
        hit = [m for m in BLOCK_MARKERS if m in text]
        if hit:
            return {"status": BLOCKED,
                    "detail": f"HTTP {code}, bot-check wording on page: '{hit[0]}'"}

    negatives = [p for p in NEGATIVE_PHRASES if p in text]
    positives = [p for p in POSITIVE_PHRASES if p in text]
    out_text = [str(p).lower() for p in r.get("out_of_stock_text", [])]
    vis_hit = [p for p in out_text if p in text]
    raw_hit = [p for p in out_text if p in raw_low]

    # 1. Retailer's own out-of-stock wording on the visible page wins.
    if vis_hit:
        return {"status": OUT_OF_STOCK,
                "detail": f"page says '{vis_hit[0]}'"}

    # 2. Product data says in stock.
    if ld and ld["status"] == IN_STOCK:
        signals = [f"page data says {ld['raw']}"]
        if positives:
            signals.append(f"page text: '{positives[0]}'")
        warn = f"BUT page text also says '{negatives[0]}'" if negatives else ""
        return {"status": IN_STOCK, "price": ld["price"], "strength": "strong",
                "signals": signals, "warn": warn,
                "detail": f"{ld['name']} | STRONG: {'; '.join(signals)} {warn}".strip()}

    # 3. Product data says out of stock.
    if ld and ld["status"] == OUT_OF_STOCK:
        return {"status": OUT_OF_STOCK,
                "detail": f"{ld['name']} | page data says {ld['raw']}"}

    # 4. No usable product data: out-of-stock phrase found in page code.
    if raw_hit:
        return {"status": OUT_OF_STOCK,
                "detail": f"'{raw_hit[0]}' found in page code (no product data)"}

    # 5. Retailer rule: in stock if its out-of-stock message is absent,
    #    but only if the page clearly loaded as the PS5 Pro product page.
    if r.get("in_stock_if_absent"):
        if not NAME_RE.search(text):
            return {"status": UNKNOWN,
                    "detail": f"'PS5 Pro' not found in page text ({len(text)} chars); "
                              "cannot confirm the page loaded properly. Check the link."}
        signals = ["retailer's out-of-stock message is absent"]
        if positives:
            signals.append(f"page text: '{positives[0]}'")
        return {"status": IN_STOCK, "price": None, "strength": "weak",
                "signals": signals, "warn": "",
                "detail": f"WEAK (message absent): {'; '.join(signals)}"}

    # 6. Generic text-only fallback.
    if ld is None or ld["status"] == UNKNOWN:
        if positives and not negatives:
            signals = [f"page text: '{positives[0]}'"]
            return {"status": IN_STOCK, "price": None, "strength": "weak",
                    "signals": signals, "warn": "",
                    "detail": f"WEAK (text only): {'; '.join(signals)}"}
        if negatives:
            return {"status": OUT_OF_STOCK,
                    "detail": f"page text says '{negatives[0]}'"}
        return {"status": UNKNOWN,
                "detail": f"No stock data or wording found (page text {len(text)} chars)"}
    return {"status": UNKNOWN, "detail": "Unclear"}


def main():
    if os.environ.get("SEND_TEST", "").lower() == "true":
        ok = send_telegram("✅ Test message from your PS5 Pro monitor. Telegram is working.")
        log("Test message sent." if ok else "Test message FAILED.")
        return 0 if ok else 1

    retailers = load_json(CONFIG_FILE, [])
    if not retailers:
        log(f"ERROR: {CONFIG_FILE} is missing, empty or invalid (see message above).")
        return 1

    state = load_json(STATE_FILE, {})
    failures = 0
    log(f"{'Retailer':<14} {'Status':<14} Detail")

    for i, r in enumerate(retailers):
        if i:
            time.sleep(1)
        name = r["name"]
        url = r.get("url", "")
        res = check_retailer(r)
        status = res["status"]
        log(f"{name:<14} {status:<14} {res.get('detail', '')}")

        s = state.get(name, {})
        s.setdefault("alerted", False)
        s.setdefault("blocked_notified", False)
        s.setdefault("unknown_streak", 0)
        s.setdefault("not_in_stock_streak", 0)

        if status == IN_STOCK:
            s["unknown_streak"] = 0
            s["not_in_stock_streak"] = 0
            s["blocked_notified"] = False
            if not s["alerted"]:
                kind = "STRONG signal" if res["strength"] == "strong" else "WEAK signal"
                msg = f"🟢 PS5 Pro may be IN STOCK at {name} ({kind})\n"
                if res.get("price"):
                    msg += f"Price: {res['price']}\n"
                msg += url + "\n"
                msg += "Signals: " + "; ".join(res["signals"]) + "\n"
                if res.get("warn"):
                    msg += f"Note: {res['warn']}\n"
                msg += "Cart buttons can be present when it is really sold out, so check the page."
                if send_telegram(msg):
                    s["alerted"] = True
                else:
                    failures += 1
        elif status == BLOCKED:
            s["not_in_stock_streak"] = 0
            if not s["blocked_notified"]:
                msg = (f"⚠️ {name} is blocking automated checks ({res.get('detail')}).\n"
                       "I am NOT treating this as in or out of stock, and I won't "
                       "try to bypass it. Check this retailer manually.")
                if send_telegram(msg):
                    s["blocked_notified"] = True
                else:
                    failures += 1
        else:  # OUT_OF_STOCK, UNKNOWN, NOT_CONFIGURED
            s["blocked_notified"] = False
            s["not_in_stock_streak"] = min(s["not_in_stock_streak"] + 1, RESET_AFTER)
            if s["not_in_stock_streak"] >= RESET_AFTER:
                s["alerted"] = False
            if status == UNKNOWN:
                s["unknown_streak"] = min(s["unknown_streak"] + 1, UNKNOWN_ALERT_AFTER + 1)
                if s["unknown_streak"] == UNKNOWN_ALERT_AFTER:
                    send_telegram(f"ℹ️ {name}: status unclear for {UNKNOWN_ALERT_AFTER} "
                                  f"checks in a row ({res.get('detail')}). Check manually.")
            else:
                s["unknown_streak"] = 0
        state[name] = s

    save_state(state)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
