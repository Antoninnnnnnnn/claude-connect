import argparse
import os
import statistics
import time
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv


def main() -> None:
    parser = argparse.ArgumentParser(description="Leboncoin API resistance test")
    parser.add_argument("--base-url", default="http://127.0.0.1:8092")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--text", default="nike")
    parser.add_argument("--category", default="sneakers")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    load_dotenv("/home/antonin/claude-connect/.env")
    api_key = os.environ.get("API_KEY", "")
    if not api_key:
        raise SystemExit("API_KEY is missing")

    params = {"text": args.text, "category": args.category, "limit": args.limit, "sort": "newest"}
    latencies: list[float] = []
    failures: list[str] = []
    total_items = 0

    with httpx.Client(timeout=90) as client:
        for index in range(1, args.count + 1):
            started = time.monotonic()
            try:
                response = client.get(
                    f"{args.base_url}/search?{urlencode(params)}",
                    headers={"X-API-Key": api_key},
                )
                elapsed = time.monotonic() - started
                latencies.append(elapsed)
                payload = response.json()
                item_count = len(payload.get("data", {}).get("items", [])) if payload.get("ok") else 0
                total_items += item_count
                status = "ok" if response.status_code == 200 and payload.get("ok") else "fail"
                print(f"{index:02d} {status} status={response.status_code} items={item_count} elapsed={elapsed:.2f}s")
                if status == "fail":
                    failures.append(f"{index}: {payload.get('error') or response.text[:180]}")
            except Exception as exc:
                elapsed = time.monotonic() - started
                latencies.append(elapsed)
                failures.append(f"{index}: {exc}")
                print(f"{index:02d} fail exception={exc} elapsed={elapsed:.2f}s")
            if index != args.count:
                time.sleep(args.delay)

    ok_count = args.count - len(failures)
    print("---")
    print(f"success={ok_count}/{args.count} failure_rate={(len(failures) / args.count) * 100:.1f}% total_items={total_items}")
    if latencies:
        print(f"latency_min={min(latencies):.2f}s latency_median={statistics.median(latencies):.2f}s latency_max={max(latencies):.2f}s")
    if failures:
        print("failures:")
        for failure in failures:
            print(f"- {failure}")


if __name__ == "__main__":
    main()
