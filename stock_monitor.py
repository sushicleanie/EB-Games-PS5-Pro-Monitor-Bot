
import os
import re
import urllib.request
import urllib.parse
import urllib.error

PAGE_URL = "https://www.ebgames.com.au/featured/playstation-5"
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message
    }).encode()

    request = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError("Telegram alert failed")

def main():
    request = urllib.request.Request(
        PAGE_URL,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="replace")

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).lower()

    if "playstation 5 pro" not in text:
        send_telegram(
            "PS5 Pro monitor: EB Games' page changed "
            "or the product was not found. Please check manually: "
            + PAGE_URL
        )
        return

    unavailable = (
        "presold out" in text
        or "out of stock" in text
        or "sold out" in text
    )

    if unavailable:
        print("PS5 Pro appears unavailable. No alert sent.")
    else:
        send_telegram(
            "PS5 Pro availability may have changed! "
            "Check the EB Games page now: " + PAGE_URL
        )

if __name__ == "__main__":
    main()
