"""Configure the owner's Telegram chat menu; does not modify existing bot webhooks."""

import json
import os
import urllib.request
from urllib.parse import urlparse


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    owner_id = int(os.environ["TELEGRAM_OWNER_ID"])
    public_url = os.environ["MINIAPP_PUBLIC_URL"].rstrip("/")
    if urlparse(public_url).scheme != "https":
        raise SystemExit("MINIAPP_PUBLIC_URL must be HTTPS")
    endpoint = "https://api.telegram.org/bot" + token + "/setChatMenuButton"
    body = {
        "chat_id": owner_id,
        "menu_button": {
            "type": "web_app",
            "text": "Talk to Hermes",
            "web_app": {"url": public_url},
        },
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise SystemExit("Telegram rejected the chat menu update: " + result.get("description", ""))
    print("Telegram Mini App chat menu configured for owner chat:", owner_id)


if __name__ == "__main__":
    main()
