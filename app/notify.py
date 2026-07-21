import os
import requests


def notify(title, message, priority="default"):
    # Push notification via ntfy. Deliberately a silent no-op until NTFY_TOPIC
    # is set in .env, so every caller can fire-and-forget without caring
    # whether notifications are configured yet.
    # NTFY_TOPIC is either a bare topic name (delivered via ntfy.sh) or a full
    # URL for a self-hosted ntfy server.
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return False
    url = topic if topic.startswith("http") else f"https://ntfy.sh/{topic}"
    try:
        requests.post(
            url,
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=10,
        )
        return True
    except requests.RequestException:
        # Notifications are best-effort — a dead ntfy server must never take
        # down a sync cycle or a page load.
        return False




#THIS IS A TEST COMMENT USE TO SEE WHERE THE NEW REPO GOES