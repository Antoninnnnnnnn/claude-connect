"""Browser-backed access to www.lacentrale.fr HTML.

DataDome blocks every plain HTTP client on www.lacentrale.fr, whatever the TLS
fingerprint, because the interstitial device check has to run in a real browser.
The measured behaviour is:

* a camoufox (patched Firefox) session, headed, through a French residential exit
  clears the check and receives a `datadome` clearance cookie;
* that cookie is **IP-portable** — replaying it with curl_cffi from the server's
  own address returns 200, no proxy needed;
* it is rate-limited, not use-limited: ~9 requests at 1/s burn it permanently,
  while 20+ requests at 5s spacing all succeed.

So we mint a cookie with the browser once (subprocess, separate virtualenv), then
serve every www HTML request with curl_cffi at a safe pace, and re-mint whenever
DataDome blocks us again.

Minting is slow (~1 min, several on retries), so it runs on a background thread:
a request waits at most `CENTRALE_BROWSER_MINT_WAIT` seconds and then fails with a
"retry shortly" error rather than holding a worker past the reverse proxy timeout.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from curl_cffi import requests

from app.config import Settings


logger = logging.getLogger(__name__)

MINT_SCRIPT = Path(__file__).resolve().parent / "mint_datadome.py"
BLOCK_MARKERS = ("captcha-delivery", "geo.captcha-delivery.com")
# camoufox prints one of these per download tick; they drown out the real error.
PROGRESS_LINE_RE = re.compile(r"^(Downloading|Extracting|Unpacking|Fetching)\b.*\d+%\s*$", re.I)


class DatadomeUnavailable(RuntimeError):
    """No usable DataDome clearance could be obtained."""


def looks_blocked(body: str) -> bool:
    head = body[:50000].lower()
    return any(marker in head for marker in BLOCK_MARKERS)


class WwwFetcher:
    def __init__(self, settings: Settings, proxy_provider: Callable[[], str | None] | None = None):
        self.settings = settings
        self._proxy_provider = proxy_provider
        self._lock = threading.Lock()
        self._throttle_lock = threading.Lock()
        self._token: dict[str, object] | None = None
        self._last_fetch = 0.0
        self._last_mint_failure = 0.0
        self._mint_count = 0
        self._mint_thread: threading.Thread | None = None
        self._mint_done: threading.Event | None = None
        self._mint_error: str | None = None
        self._load_token()

    # ------------------------------------------------------------------ state

    def available(self) -> bool:
        return bool(self.settings.centrale_browser_enabled) and Path(self.settings.centrale_browser_python).exists()

    def status(self) -> dict[str, object]:
        with self._lock:
            token = self._token
            age = None
            if token:
                age = round(time.time() - float(token.get("minted_at") or 0.0), 1)
            minting = self._mint_thread is not None and self._mint_thread.is_alive()
            cooldown_left = 0.0
            if self._last_mint_failure:
                cooldown_left = max(
                    0.0,
                    float(self.settings.centrale_browser_mint_cooldown)
                    - (time.time() - self._last_mint_failure),
                )
            return {
                "enabled": bool(self.settings.centrale_browser_enabled),
                "browser_python": self.settings.centrale_browser_python,
                "browser_available": self.available(),
                "has_token": token is not None,
                "token_age_s": age,
                "mints": self._mint_count,
                "minting": minting,
                "mint_error": self._mint_error,
                "mint_cooldown_s": round(cooldown_left, 1) or None,
            }

    def _token_path(self) -> Path:
        return Path(self.settings.centrale_datadome_token_file)

    def _load_token(self) -> None:
        path = self._token_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict) and isinstance(data.get("cookies"), dict):
            self._token = data

    def _store_token(self, token: dict[str, object]) -> None:
        path = self._token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(token), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _token_expired(self, token: dict[str, object]) -> bool:
        max_age = float(self.settings.centrale_datadome_token_max_age)
        if max_age <= 0:
            return False
        return (time.time() - float(token.get("minted_at") or 0.0)) > max_age

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
        try:
            self._token_path().unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------- mint

    def _run_mint(self) -> dict[str, object]:
        """Spawn the browser and return a fresh token. Holds no lock."""
        proxy = self.settings.centrale_browser_proxy or self.settings.centrale_proxy or ""
        cmd: list[str] = []
        if self.settings.centrale_browser_xvfb:
            cmd += ["xvfb-run", "-a"]
        cmd += [
            self.settings.centrale_browser_python,
            str(MINT_SCRIPT),
            "",  # placeholder, replaced below
            proxy,
            str(self.settings.centrale_browser_mint_attempts),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "token.json"
            cmd[cmd.index("")] = str(out_path)
            logger.info("Minting DataDome cookie via browser (proxy=%s)", bool(proxy))
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=float(self.settings.centrale_browser_mint_timeout),
                    cwd=str(MINT_SCRIPT.parent.parent),
                )
            except subprocess.TimeoutExpired:
                raise DatadomeUnavailable("browser mint timed out") from None
            if proc.returncode != 0 or not out_path.exists():
                raise DatadomeUnavailable(f"browser mint failed: {self._mint_failure_detail(proc)}")
            return json.loads(out_path.read_text(encoding="utf-8"))

    @staticmethod
    def _mint_failure_detail(proc: "subprocess.CompletedProcess[str]") -> str:
        """Summarise a failed mint run.

        camoufox floods stderr with addon download progress, which used to fill the
        300-char head of the message and hide the actual exception. Progress lines are
        dropped and the tail is kept, because that is where the error lands.
        """
        lines: list[str] = []
        for stream in (proc.stderr, proc.stdout):
            for line in (stream or "").splitlines():
                clean = line.strip()
                if not clean or PROGRESS_LINE_RE.match(clean):
                    continue
                if clean not in lines:
                    lines.append(clean)
        if not lines:
            return f"no diagnostic output (exit {proc.returncode})"
        return " | ".join(lines[-4:])[:500]

    def _mint_worker(self) -> None:
        """Background mint: never runs on a request thread."""
        try:
            token = self._run_mint()
        except DatadomeUnavailable as exc:
            with self._lock:
                self._last_mint_failure = time.time()
                self._mint_error = str(exc)
            logger.warning("DataDome mint failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 - subprocess boundary, keep the thread quiet
            with self._lock:
                self._last_mint_failure = time.time()
                self._mint_error = f"{exc.__class__.__name__}: {exc}"
            logger.warning("DataDome mint crashed: %s", exc)
        else:
            self._store_token(token)
            with self._lock:
                self._token = token
                self._last_mint_failure = 0.0
                self._mint_error = None
                self._mint_count += 1
            logger.info("DataDome cookie minted (%d cookies)", len(token.get("cookies") or {}))
        finally:
            with self._lock:
                done = self._mint_done
                self._mint_thread = None
            if done is not None:
                done.set()

    def _start_mint_locked(self) -> threading.Event:
        """Ensure a mint is running and return the event that marks its end.

        Single-flight: concurrent callers share one browser run. Caller holds `_lock`.
        """
        if self._mint_thread is not None and self._mint_thread.is_alive():
            return self._mint_done  # type: ignore[return-value]
        if not self.available():
            raise DatadomeUnavailable(f"browser python not found: {self.settings.centrale_browser_python}")
        cooldown = float(self.settings.centrale_browser_mint_cooldown)
        since_failure = time.time() - self._last_mint_failure
        if self._last_mint_failure and since_failure < cooldown:
            raise DatadomeUnavailable(
                f"mint cooling down ({cooldown - since_failure:.0f}s left)"
                + (f": {self._mint_error}" if self._mint_error else "")
            )
        done = threading.Event()
        thread = threading.Thread(target=self._mint_worker, name="datadome-mint", daemon=True)
        self._mint_done = done
        self._mint_thread = thread
        thread.start()
        return done

    def _ensure_token(self, force: bool = False) -> dict[str, object]:
        """Return a usable token, waiting only a bounded amount for a fresh mint.

        Minting takes ~1 min and can retry for several, which is far longer than the
        reverse proxy's read timeout. So the browser runs on its own thread and a
        request waits at most `centrale_browser_mint_wait` seconds before giving up;
        the mint keeps going and the next request picks up the result.
        """
        with self._lock:
            token = self._token
            if token and not force and not self._token_expired(token):
                return token
            done = self._start_mint_locked()

        budget = max(0.0, float(self.settings.centrale_browser_mint_wait))
        if not done.wait(timeout=budget):
            raise DatadomeUnavailable(
                f"DataDome clearance is being minted in the background "
                f"(waited {budget:.0f}s); retry shortly"
            )

        with self._lock:
            token = self._token
            error = self._mint_error
        if token is not None and not self._token_expired(token):
            return token
        raise DatadomeUnavailable(error or "no DataDome clearance available")

    # ------------------------------------------------------------------ fetch

    def _throttle(self) -> None:
        """Pace www fetches on a dedicated lock.

        `_lock` also serialises minting and token reads; sleeping under it made a
        status() call or a concurrent token check wait out the full interval.
        """
        interval = max(0.0, float(self.settings.centrale_www_min_interval))
        if interval <= 0:
            return
        with self._throttle_lock:
            wait = interval - (time.monotonic() - self._last_fetch)
            if wait > 0:
                time.sleep(wait)
            self._last_fetch = time.monotonic()

    def get(self, url: str, headers: dict[str, str] | None = None) -> requests.Response:
        """Fetch a www.lacentrale.fr URL, re-minting clearance on a block."""
        last_error: str | None = None
        for attempt in range(2):
            token = self._ensure_token(force=attempt > 0)
            cookies = token.get("cookies") or {}
            user_agent = str(token.get("ua") or "")
            proxy = self._proxy_provider() if self._proxy_provider else None
            session = requests.Session(
                impersonate=self.settings.centrale_www_impersonate,
                proxies={"http": proxy, "https": proxy} if proxy else None,
            )
            try:
                merged = {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                    "Upgrade-Insecure-Requests": "1",
                }
                merged.update(headers or {})
                if user_agent:
                    merged["User-Agent"] = user_agent
                session.headers.update(merged)
                for name, value in cookies.items():
                    session.cookies.set(str(name), str(value), domain=".lacentrale.fr")
                self._throttle()
                response = session.get(url, timeout=self.settings.centrale_timeout, allow_redirects=True)
                body = response.text if isinstance(response.text, str) else ""
                if response.status_code in {403, 429} or looks_blocked(body):
                    last_error = f"blocked: HTTP {response.status_code}"
                    logger.warning("www fetch blocked (%s), clearance burnt: %s", response.status_code, url)
                    self.invalidate()
                    continue
                return response
            except DatadomeUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - retry once with fresh clearance
                last_error = f"{exc.__class__.__name__}: {exc}"
            finally:
                session.close()
        raise DatadomeUnavailable(last_error or "www fetch failed")


def main(argv: list[str]) -> int:
    """Manual check: python -m app.www_session [url]"""
    logging.basicConfig(level=logging.INFO)
    from app.config import get_settings

    url = argv[1] if len(argv) > 1 else "https://www.lacentrale.fr/listing"
    fetcher = WwwFetcher(get_settings())
    response = fetcher.get(url)
    print(json.dumps({"status": response.status_code, "length": len(response.text), **fetcher.status()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
