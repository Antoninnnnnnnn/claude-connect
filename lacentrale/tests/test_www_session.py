"""DataDome clearance: block detection, token lifetime and the background mint.

The mint used to run inline and could hold a request worker for up to 420s while
nginx cuts at 90s. These tests pin the bounded wait, the single-flight guarantee
and the cooldown.
"""

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.centrale_client import CentraleClient
from app.www_session import DatadomeUnavailable, WwwFetcher, looks_blocked


@pytest.fixture
def fetcher(settings) -> WwwFetcher:
    settings.centrale_browser_enabled = True
    # Point at an interpreter that exists so available() is True without a browser.
    settings.centrale_browser_python = "/bin/sh"
    settings.centrale_browser_mint_wait = 0.3
    settings.centrale_browser_mint_cooldown = 60
    return WwwFetcher(settings)


# ------------------------------------------------------------ block detection


@pytest.mark.parametrize(
    "body",
    [
        "<html>captcha-delivery</html>",
        "<script src='https://geo.captcha-delivery.com/captcha/'></script>",
    ],
)
def test_looks_blocked_detects_captcha(body):
    assert looks_blocked(body) is True


def test_looks_blocked_passes_normal_page():
    assert looks_blocked("<html><body>RENAULT ZOE</body></html>") is False


def test_looks_blocked_only_scans_the_head():
    """Bounded scan: a marker past 50k chars is deliberately not searched."""
    assert looks_blocked("x" * 60000 + "captcha-delivery") is False


def test_client_block_detection_variants(client):
    assert client._looks_blocked("please enable JS and disable any ad blocker") is True
    assert client._looks_blocked('var dd={"rt":"c"} captcha') is True
    assert client._looks_blocked("datadome blocked this request") is True


def test_client_block_detection_ignores_datadome_on_real_page(client):
    """A real SSR page mentions datadome in its script tags; that is not a block."""
    body = '<script id="__NEXT_DATA__">{"datadome":"x","captcha":false}</script>'
    assert client._looks_blocked(body) is False


# ----------------------------------------------------------------- token state


def test_no_token_initially(fetcher):
    assert fetcher.status()["has_token"] is False


def test_token_loaded_from_disk(settings):
    path = Path(settings.centrale_datadome_token_file)
    path.write_text(json.dumps({"cookies": {"datadome": "abc"}, "minted_at": time.time()}))
    assert WwwFetcher(settings).status()["has_token"] is True


def test_malformed_token_file_ignored(settings):
    Path(settings.centrale_datadome_token_file).write_text("{not json")
    assert WwwFetcher(settings).status()["has_token"] is False


def test_token_without_cookies_ignored(settings):
    Path(settings.centrale_datadome_token_file).write_text(json.dumps({"minted_at": 1}))
    assert WwwFetcher(settings).status()["has_token"] is False


def test_token_expiry(fetcher):
    fetcher.settings.centrale_datadome_token_max_age = 60
    assert fetcher._token_expired({"minted_at": time.time()}) is False
    assert fetcher._token_expired({"minted_at": time.time() - 120}) is True


def test_token_never_expires_when_max_age_zero(fetcher):
    fetcher.settings.centrale_datadome_token_max_age = 0
    assert fetcher._token_expired({"minted_at": 0}) is False


def test_invalidate_removes_token_and_file(settings):
    path = Path(settings.centrale_datadome_token_file)
    path.write_text(json.dumps({"cookies": {"datadome": "x"}, "minted_at": time.time()}))
    instance = WwwFetcher(settings)
    instance.invalidate()
    assert instance.status()["has_token"] is False
    assert not path.exists()


def test_stored_token_is_owner_only(fetcher):
    fetcher._store_token({"cookies": {"datadome": "x"}, "minted_at": time.time()})
    mode = Path(fetcher.settings.centrale_datadome_token_file).stat().st_mode & 0o777
    assert mode == 0o600, "clearance cookie must not be world readable"


def test_fresh_token_short_circuits_minting(fetcher, monkeypatch):
    token = {"cookies": {"datadome": "x"}, "minted_at": time.time()}
    fetcher._token = token
    monkeypatch.setattr(
        fetcher, "_run_mint", lambda: pytest.fail("must not mint with a fresh token")
    )
    assert fetcher._ensure_token() is token


# ------------------------------------------------------------- background mint


def test_mint_wait_is_bounded(fetcher, monkeypatch):
    """A slow browser must not hold the caller past the configured budget."""
    monkeypatch.setattr(fetcher, "_run_mint", lambda: (time.sleep(5), {})[1])

    started = time.monotonic()
    with pytest.raises(DatadomeUnavailable, match="retry shortly"):
        fetcher._ensure_token()
    elapsed = time.monotonic() - started

    assert elapsed < 2, f"caller blocked {elapsed:.1f}s despite a 0.3s budget"


def test_mint_keeps_running_after_the_caller_gives_up(fetcher, monkeypatch):
    def slow_mint():
        time.sleep(0.6)
        return {"cookies": {"datadome": "late"}, "minted_at": time.time()}

    monkeypatch.setattr(fetcher, "_run_mint", slow_mint)
    with pytest.raises(DatadomeUnavailable):
        fetcher._ensure_token()

    # The browser run survives to serve the next request.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not fetcher.status()["has_token"]:
        time.sleep(0.05)
    assert fetcher.status()["has_token"] is True
    assert fetcher._ensure_token()["cookies"]["datadome"] == "late"


def test_mint_is_single_flight(fetcher, monkeypatch):
    calls = []

    def counting_mint():
        calls.append(1)
        time.sleep(0.4)
        return {"cookies": {"datadome": "x"}, "minted_at": time.time()}

    monkeypatch.setattr(fetcher, "_run_mint", counting_mint)

    def attempt():
        try:
            fetcher._ensure_token()
        except DatadomeUnavailable:
            pass

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(calls) == 1, f"{len(calls)} browser runs for 4 concurrent callers"


def test_mint_failure_sets_cooldown(fetcher, monkeypatch):
    monkeypatch.setattr(
        fetcher, "_run_mint", lambda: (_ for _ in ()).throw(DatadomeUnavailable("boom"))
    )
    fetcher.settings.centrale_browser_mint_wait = 2

    with pytest.raises(DatadomeUnavailable, match="boom"):
        fetcher._ensure_token()

    status = fetcher.status()
    assert status["mint_error"] == "boom"
    assert status["mint_cooldown_s"] is not None


def test_cooldown_blocks_further_mints(fetcher, monkeypatch):
    fetcher._last_mint_failure = time.time()
    fetcher._mint_error = "previous failure"
    monkeypatch.setattr(fetcher, "_run_mint", lambda: pytest.fail("cooldown must block minting"))

    with pytest.raises(DatadomeUnavailable, match="cooling down"):
        fetcher._ensure_token()


def test_missing_browser_python_reports_clearly(settings):
    settings.centrale_browser_enabled = True
    settings.centrale_browser_python = "/nonexistent/python"
    instance = WwwFetcher(settings)
    assert instance.available() is False
    with pytest.raises(DatadomeUnavailable, match="browser python not found"):
        instance._ensure_token()


def test_successful_mint_clears_error_and_counts(fetcher, monkeypatch):
    fetcher.settings.centrale_browser_mint_wait = 3
    monkeypatch.setattr(
        fetcher, "_run_mint", lambda: {"cookies": {"datadome": "ok"}, "minted_at": time.time()}
    )
    token = fetcher._ensure_token()
    assert token["cookies"]["datadome"] == "ok"
    status = fetcher.status()
    assert status["mints"] == 1
    assert status["mint_error"] is None
    assert status["minting"] is False


# -------------------------------------------------- mint failure diagnostics


def completed(stdout="", stderr="", code=1):
    return subprocess.CompletedProcess(args=["x"], returncode=code, stdout=stdout, stderr=stderr)


def test_failure_detail_strips_download_progress():
    """Progress spam used to fill the message and hide the real exception."""
    noise = "\n".join(f"Downloading addon (UBO): {pct}%" for pct in range(0, 100))
    real = "attempt 0: CamoufoxNotInstalled: official/stable is not installed."
    detail = WwwFetcher._mint_failure_detail(completed(stderr=f"{noise}\n{real}"))
    assert "CamoufoxNotInstalled" in detail
    assert "Downloading addon" not in detail


def test_failure_detail_keeps_the_tail():
    lines = "\n".join(f"line {index}" for index in range(20))
    detail = WwwFetcher._mint_failure_detail(completed(stderr=lines))
    assert "line 19" in detail
    assert "line 0" not in detail


def test_failure_detail_falls_back_to_stdout():
    detail = WwwFetcher._mint_failure_detail(completed(stdout="only on stdout"))
    assert "only on stdout" in detail


def test_failure_detail_without_output():
    detail = WwwFetcher._mint_failure_detail(completed(code=3))
    assert "no diagnostic output" in detail and "3" in detail


def test_failure_detail_is_bounded():
    detail = WwwFetcher._mint_failure_detail(completed(stderr="y" * 5000))
    assert len(detail) <= 500


def test_failure_detail_dedupes_repeated_lines():
    detail = WwwFetcher._mint_failure_detail(completed(stderr="same\nsame\nsame\nreal error"))
    assert detail.count("same") == 1


# ---------------------------------------------------------------- pacing split


def test_www_throttle_does_not_block_status(fetcher):
    """Regression: pacing slept under the lock that status() and token reads need."""
    fetcher.settings.centrale_www_min_interval = 1.0
    fetcher._throttle()  # arm the interval

    throttling = threading.Thread(target=fetcher._throttle, daemon=True)
    throttling.start()
    time.sleep(0.05)

    started = time.monotonic()
    fetcher.status()
    elapsed = time.monotonic() - started

    assert elapsed < 0.3, f"status() blocked {elapsed:.2f}s behind the throttle"
    throttling.join(timeout=3)


# ---------------------------------------------------- client wiring / shutdown


def test_client_health_exposes_browser_state(client):
    status = client.health_status()
    assert "www_browser" in status
    assert status["max_fetchable_limit"] == client.settings.max_fetchable_limit()


def test_client_close_is_idempotent(client):
    client.close()
    client.close()


def test_listing_reference_format_is_validated(client):
    with pytest.raises(Exception):
        client.listing("not-a-ref")


def test_listings_rejects_bad_reference_before_any_fetch(client):
    with pytest.raises(Exception):
        client.listings(["E1", "bad ref"])


def test_listings_empty_input_short_circuits(client):
    assert client.listings([]) == {"items": [], "errors": {}}


def test_listing_cache_key_is_flag_sensitive():
    key_a = CentraleClient._listing_cache_key("E1", include_description=True)
    key_b = CentraleClient._listing_cache_key("E1", include_description=False)
    assert key_a != key_b, "cached payloads differ by flag, keys must too"


def test_listing_cache_key_is_order_insensitive():
    assert CentraleClient._listing_cache_key(
        "E1", include_image=True, include_dealer=False
    ) == CentraleClient._listing_cache_key("E1", include_dealer=False, include_image=True)
