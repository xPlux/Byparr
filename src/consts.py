import logging
import os
import sys
from pathlib import Path

from playwright_captcha import CaptchaType
from playwright_captcha.utils.camoufox_add_init_script.add_init_script import (
    get_addon_path,
)

LOG_LEVEL = logging.getLevelNamesMapping()[os.getenv("LOG_LEVEL", "INFO").upper()]

VERSION = os.getenv("VERSION", "unknown").removeprefix("v")

ADDON_PATH = str(Path(get_addon_path()).absolute())
MAX_ATTEMPTS = sys.maxsize

PROXY_SERVER = os.getenv("PROXY_SERVER")
PROXY_USERNAME = os.getenv("PROXY_USERNAME")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD")

# How many requests share the same Camoufox process before it is restarted
# with a fresh fingerprint. 1 = restart every request (legacy behavior),
# 0 or negative = never rotate (keep one persona for the container lifetime).
FINGERPRINT_ROTATE_EVERY = int(os.getenv("FINGERPRINT_ROTATE_EVERY", "1"))

# When a Camoufox instance is reused across requests, clear cookies and
# permissions plus open a fresh tab between requests. Has no effect when
# FINGERPRINT_ROTATE_EVERY == 1, because the instance is recreated anyway.
FINGERPRINT_CLEAR_BETWEEN = os.getenv(
    "FINGERPRINT_CLEAR_BETWEEN", "true"
).strip().lower() in {"1", "true", "yes", "on"}

# Hard cap (seconds) for closing/restarting the shared Camoufox instance.
# If teardown hangs past this, the shared state is force-reset so the busy
# flag is always released and the next request can spawn a fresh browser.
BROWSER_SHUTDOWN_TIMEOUT = int(os.getenv("BROWSER_SHUTDOWN_TIMEOUT", "20"))


HOST = os.getenv("HOST", "0.0.0.0")  # noqa: S104
PORT = int(os.getenv("PORT", "8191"))

CHALLENGE_TITLES_MAP: dict[CaptchaType, list[str]] = {
    # Cloudflare
    CaptchaType.CLOUDFLARE_INTERSTITIAL: ["Just a moment..."],
}

CHALLENGE_TITLES = [
    title for titles in CHALLENGE_TITLES_MAP.values() for title in titles
]
