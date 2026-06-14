import argparse
import os
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="La Centrale API resistance test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8094")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--make", default="RENAULT")
    parser.add_argument("--model", default="ZOE")
    parser.add_argument("--version", default="intens")
    parser.add_argument("--price-max", type=float, default=8000)
    parser.add_argument("--zip", default="27000")
    parser.add_argument("--distance-km", type=int, default=5)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    load_dotenv(ROOT.parent / ".env")
    api_key = os.environ.get("API_KEY", "")
    if not api_key:
        raise SystemExit("API_KEY is missing")

    headers = {"X-API-Key": api_key}
    search_params = {
        "make": args.make,
        "model": args.model,
        "version": args.version,
        "price_max": args.price_max,
        "zip": args.zip,
        "distance_km": args.distance_km,
        "limit": args.limit,
    }

    endpoints = [
        ("health", "GET", "/health", None, False),
        ("metadata", "GET", "/metadata", {"make": args.make, "model": args.model}, True),
        ("search", "GET", f"/search?{urlencode(search_params)}", None, True),
        ("price-stats", "GET", f"/price-stats?{urlencode(search_params)}", None, True),
    ]

    latencies: dict[str, list[float]] = {name: [] for name, *_ in endpoints}
    failures: dict[str, list[str]] = {name: [] for name, *_ in endpoints}
    totals: dict[str, int] = {name: 0 for name, *_ in endpoints}

    with httpx.Client(timeout=90, base_url=args.base_url) as client:
        for label, path, expected_status in [
            ("auth-missing", "/search", 401),
            ("auth-invalid", "/search", 401),
        ]:
            auth_headers = None if label == "auth-missing" else {"X-API-Key": "invalid-key"}
            response = client.get(path, headers=auth_headers)
            ok = response.status_code == expected_status
            print(f"{label:12} status={response.status_code} expected={expected_status} ok={ok}")
            if not ok:
                failures.setdefault("auth", []).append(
                    f"{label}: HTTP {response.status_code} body={response.text[:200]}"
                )

        invalid_sort = client.get("/search?sort=invalid", headers=headers)
        sort_ok = invalid_sort.status_code == 422
        print(f"sort-invalid status={invalid_sort.status_code} expected=422 ok={sort_ok}")
        if not sort_ok:
            failures.setdefault("validation", []).append(
                f"sort-invalid: HTTP {invalid_sort.status_code}"
            )

        for index in range(1, args.count + 1):
            print(f"--- run {index}/{args.count} ---")
            for name, method, path, json_body, auth in endpoints:
                started = time.monotonic()
                try:
                    response = client.request(
                        method,
                        path,
                        headers=headers if auth else None,
                        json=json_body,
                    )
                    elapsed = time.monotonic() - started
                    latencies[name].append(elapsed)
                    payload = response.json()
                    ok = response.status_code == 200 and payload.get("ok") is True
                    if not ok:
                        failures[name].append(f"run {index}: HTTP {response.status_code} {payload}")
                    else:
                        totals[name] += 1
                    print(f"{name:12} status={response.status_code} ok={ok} elapsed={elapsed:.2f}s")
                except Exception as exc:
                    failures[name].append(f"run {index}: {exc}")
                    print(f"{name:12} FAIL {exc}")
            if index < args.count:
                time.sleep(args.delay)

        listing_ref = None
        try:
            search_response = client.get(f"/search?{urlencode(search_params)}", headers=headers)
            items = (search_response.json().get("data") or {}).get("items") or []
            if items:
                listing_ref = items[0].get("id")
        except Exception:
            listing_ref = None

        if listing_ref:
            started = time.monotonic()
            try:
                response = client.get(f"/listing/{listing_ref}", headers=headers)
                elapsed = time.monotonic() - started
                latencies.setdefault("listing", []).append(elapsed)
                payload = response.json()
                ok = response.status_code == 200 and payload.get("ok") is True
                if ok:
                    totals["listing"] = totals.get("listing", 0) + 1
                else:
                    failures.setdefault("listing", []).append(f"HTTP {response.status_code} {payload}")
                print(f"listing      ref={listing_ref} status={response.status_code} ok={ok} elapsed={elapsed:.2f}s")
            except Exception as exc:
                failures.setdefault("listing", []).append(str(exc))
                print(f"listing      FAIL {exc}")

    print("--- summary ---")
    all_failed = False
    for name, *_ in endpoints:
        success = totals[name]
        failure_count = len(failures[name])
        runs = success + failure_count
        lats = latencies[name]
        print(
            f"{name:12} success={success}/{runs} "
            f"median={statistics.median(lats):.2f}s max={max(lats) if lats else 0:.2f}s"
        )
        for failure in failures[name][:3]:
            print(f"  - {failure}")
        if failure_count:
            all_failed = True

    for extra in ("auth", "validation", "listing"):
        if failures.get(extra):
            all_failed = True
            print(f"{extra:12} failures={len(failures[extra])}")
            for failure in failures[extra][:3]:
                print(f"  - {failure}")

    return 1 if all_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
