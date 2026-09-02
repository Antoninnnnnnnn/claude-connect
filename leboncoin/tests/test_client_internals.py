"""Cache, proxy rotation and the throttle/cache lock separation."""

import threading
import time

from app.config import Settings
from app.lbc_client import LeboncoinClient


# ------------------------------------------------------------------- json cache


def test_cache_roundtrip(client):
    client._store_json("k", {"v": 1})
    assert client._cached_json("k") == {"v": 1}


def test_cache_miss_is_none(client):
    assert client._cached_json("absent") is None


def test_cache_expires(settings):
    settings.lbc_cache_ttl = 0.05
    client = LeboncoinClient(settings)
    client._store_json("k", {"v": 1})
    time.sleep(0.08)
    assert client._cached_json("k") is None


def test_expired_entry_is_evicted_on_read(settings):
    settings.lbc_cache_ttl = 0.05
    client = LeboncoinClient(settings)
    client._store_json("k", {"v": 1})
    time.sleep(0.08)
    client._cached_json("k")
    assert "k" not in client._next_data_cache


def test_cache_disabled_by_zero_ttl(settings):
    settings.lbc_cache_ttl = 0
    client = LeboncoinClient(settings)
    client._store_json("k", {"v": 1})
    assert client._cached_json("k") is None


def test_cache_evicts_oldest_beyond_max_entries(client):
    """conftest pins max_entries to 3."""
    for index in range(3):
        client._store_json(f"k{index}", {"v": index})
        time.sleep(0.01)  # distinct timestamps so "oldest" is unambiguous
    client._store_json("k3", {"v": 3})
    assert len(client._next_data_cache) <= 3
    assert client._cached_json("k0") is None, "oldest entry should have been dropped"
    assert client._cached_json("k3") == {"v": 3}


# ---------------------------------------------------------------- proxy pool


def make_client(**overrides) -> LeboncoinClient:
    base = dict(_env_file=None, API_KEY="k", LBC_MIN_INTERVAL=0)
    base.update(overrides)
    return LeboncoinClient(Settings(**base))


def test_no_proxy_configured_yields_direct():
    assert make_client().settings.proxy_urls() == []
    assert make_client()._proxy_candidates() == [None]


def test_single_proxy_has_no_direct_fallback_by_default():
    client = make_client(LBC_PROXIES="http://p1")
    assert client._proxy_candidates() == ["http://p1"]


def test_direct_fallback_appended_when_enabled():
    client = make_client(LBC_PROXIES="http://p1", LBC_ALLOW_DIRECT_FALLBACK=True)
    assert client._proxy_candidates() == ["http://p1", None]


def test_rotation_advances_starting_proxy():
    """Without rotation a healthy first proxy would serve every single call."""
    client = make_client(LBC_PROXIES="http://p1,http://p2,http://p3")
    assert client._proxy_candidates()[0] == "http://p1"
    assert client._proxy_candidates()[0] == "http://p2"
    assert client._proxy_candidates()[0] == "http://p3"
    assert client._proxy_candidates()[0] == "http://p1", "index must wrap"


def test_rotation_keeps_whole_pool_available_for_retries():
    client = make_client(LBC_PROXIES="http://p1,http://p2,http://p3")
    client._proxy_candidates()  # advance to p2
    assert sorted(client._proxy_candidates()) == ["http://p1", "http://p2", "http://p3"]


def test_rotation_disabled_pins_first_proxy():
    client = make_client(
        LBC_PROXIES="http://p1,http://p2", LBC_ROTATE_PROXY_PER_REQUEST=False
    )
    assert client._proxy_candidates()[0] == "http://p1"
    assert client._proxy_candidates()[0] == "http://p1"


def test_proxy_list_accepts_newline_and_semicolon_separators():
    client = make_client(LBC_PROXIES="http://p1;http://p2\nhttp://p3")
    assert client.settings.proxy_urls() == ["http://p1", "http://p2", "http://p3"]


def test_blank_proxy_env_becomes_none():
    assert make_client(LBC_PROXY="   ").settings.lbc_proxy is None


def test_duplicate_proxies_are_deduplicated():
    client = make_client(LBC_PROXIES="http://p1", LBC_PROXY="http://p1")
    assert client.settings.proxy_urls() == ["http://p1"]


# ------------------------------------------------- throttle / cache lock split


def test_throttle_does_not_block_cache_reads():
    """Regression: pacing used to sleep while holding the cache lock.

    A cache hit had to wait out another request's throttle interval. Pacing and
    state now use separate locks, so a read stays fast under a running throttle.
    """
    client = make_client(LBC_MIN_INTERVAL=1.0)
    client._store_json("warm", {"v": 1})
    client._throttle()  # arm the interval so the next call has to sleep

    throttling = threading.Thread(target=client._throttle, daemon=True)
    throttling.start()
    time.sleep(0.05)  # ensure the thread is inside its sleep

    started = time.monotonic()
    assert client._cached_json("warm") == {"v": 1}
    elapsed = time.monotonic() - started

    assert elapsed < 0.3, f"cache read blocked for {elapsed:.2f}s behind the throttle"
    throttling.join(timeout=3)


def test_throttle_still_paces_sequential_calls():
    """The separation must not weaken the rate limit itself."""
    client = make_client(LBC_MIN_INTERVAL=0.3)
    client._throttle()
    started = time.monotonic()
    client._throttle()
    assert time.monotonic() - started >= 0.25


def test_throttle_serialises_concurrent_callers():
    client = make_client(LBC_MIN_INTERVAL=0.2)
    started = time.monotonic()
    threads = [threading.Thread(target=client._throttle) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    # Three calls at 0.2s spacing cannot all finish instantly.
    assert time.monotonic() - started >= 0.3


# -------------------------------------------------------------------- shutdown


def test_close_clears_cache_and_session(client):
    client._store_json("k", {"v": 1})
    client.close()
    assert client._next_data_cache == {}
    assert client._session is None
