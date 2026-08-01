"""Mint a DataDome clearance cookie for www.lacentrale.fr using camoufox.

Runs as a standalone subprocess under the browser virtualenv (camoufox is not a
dependency of the API venv). Writes {"cookies": {...}, "ua": "...", "minted_at": ...}
to the output path given on the command line.

Usage: python mint_datadome.py <output.json> [proxy_url] [attempts]

The interstitial device check only auto-clears in a headed browser, so this is
meant to be wrapped in `xvfb-run -a` on a headless host.
"""

from __future__ import annotations

import json
import sys
import time
from urllib.parse import urlparse

HOME = "https://www.lacentrale.fr/"
BLOCK_MARKER = "captcha-delivery"


def parse_proxy(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.hostname:
        return None
    server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
    if parsed.port:
        server = f"{server}:{parsed.port}"
    proxy = {"server": server}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def clear_interstitial(page, tries: int = 10, delay_ms: int = 3000) -> bool:
    for _ in range(tries):
        if BLOCK_MARKER not in page.content():
            return True
        page.wait_for_timeout(delay_ms)
    return BLOCK_MARKER not in page.content()


def mint(proxy: dict[str, str] | None, attempts: int) -> dict[str, object]:
    from camoufox.sync_api import Camoufox

    errors: list[str] = []
    for attempt in range(attempts):
        try:
            with Camoufox(
                headless=False,
                humanize=True,
                locale="fr-FR",
                os="windows",
                proxy=proxy,
                geoip=bool(proxy),
            ) as browser:
                page = browser.new_page()
                page.goto(HOME, wait_until="load", timeout=90000)
                if not clear_interstitial(page):
                    errors.append(f"attempt {attempt}: still blocked")
                    continue
                cookies = {c["name"]: c["value"] for c in page.context.cookies()}
                if "datadome" not in cookies:
                    errors.append(f"attempt {attempt}: no datadome cookie")
                    continue
                return {
                    "cookies": cookies,
                    "ua": page.evaluate("() => navigator.userAgent"),
                    "minted_at": time.time(),
                }
        except Exception as exc:  # noqa: BLE001 - report and retry with a new exit IP
            errors.append(f"attempt {attempt}: {exc.__class__.__name__}: {exc}")
    raise RuntimeError("; ".join(errors) or "mint failed")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: mint_datadome.py <output.json> [proxy_url] [attempts]", file=sys.stderr)
        return 2
    out_path = argv[1]
    proxy = parse_proxy(argv[2] if len(argv) > 2 else None)
    attempts = int(argv[3]) if len(argv) > 3 else 3
    try:
        token = mint(proxy, attempts)
    except Exception as exc:  # noqa: BLE001 - subprocess boundary
        print(str(exc), file=sys.stderr)
        return 1
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(token, handle)
    print(f"ok cookies={len(token['cookies'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
