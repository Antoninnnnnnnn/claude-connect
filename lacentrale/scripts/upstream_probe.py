#!/usr/bin/env python3
"""Phase 0 — validate La Centrale upstream paths from this VPS."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.centrale_client import (  # noqa: E402
    CentraleClient,
    LISTING_JS_URL,
    SSR_MARKERS,
    extract_upstream_api_key,
    scan_ssr_markers,
)

SAMPLE_LISTING_URL = (
    "https://www.lacentrale.fr/listing?"
    + urlencode(
        {
            "makesModelsCommercialNames": "RENAULT::ZOE",
            "priceMax": "8000",
            "versions": "intens",
            "dptCp": "27000",
            "distance": "5",
        }
    )
)


def main() -> None:
    settings = get_settings()
    client = CentraleClient(settings)
    results: dict[str, object] = {"steps": []}

    print("=== La Centrale upstream probe ===\n")

    # 1. API key from JS
    try:
        api_key = extract_upstream_api_key(client)
        results["upstream_api_key_found"] = bool(api_key)
        print(f"[1] upstream API key from JS: {'found' if api_key else 'missing'}")
        results["steps"].append({"step": "api_key", "ok": bool(api_key)})
    except Exception as exc:
        print(f"[1] upstream API key: FAIL — {exc}")
        results["steps"].append({"step": "api_key", "ok": False, "error": str(exc)})

    # 2. Warm-up (direct — proxy often blocked on www)
    try:
        warmup = client.warmup()
        results["warmup"] = warmup
        print(f"[2] warm-up www.lacentrale.fr: status={warmup.get('status')} datadome={warmup.get('has_datadome')}")
        results["steps"].append({"step": "warmup", "ok": warmup.get("status") == 200})
    except Exception as exc:
        print(f"[2] warm-up: FAIL — {exc}")
        results["steps"].append({"step": "warmup", "ok": False, "error": str(exc)})

    # 3. Geoloc
    try:
        geo = client.probe_geoloc("27000")
        results["geoloc"] = geo
        print(f"[3] geoloc 27000: status={geo.get('status')} keys={list((geo.get('geoloc') or {}).keys())}")
        results["steps"].append({"step": "geoloc", "ok": geo.get("status") == 200})
    except Exception as exc:
        print(f"[3] geoloc: FAIL — {exc}")
        results["steps"].append({"step": "geoloc", "ok": False, "error": str(exc)})

    # 4. Aggregations smoke
    try:
        aggs = client.probe_aggregations(
            makes_models="RENAULT::ZOE",
            price_max=8000,
            version="intens",
            zip_code="27000",
        )
        results["aggregations"] = {
            "status": aggs.get("status"),
            "total": (aggs.get("body") or {}).get("total"),
        }
        print(f"[4] aggregations: status={aggs.get('status')} total={results['aggregations']['total']}")
        results["steps"].append({"step": "aggregations", "ok": aggs.get("status") == 200})
    except Exception as exc:
        print(f"[4] aggregations: FAIL — {exc}")
        results["steps"].append({"step": "aggregations", "ok": False, "error": str(exc)})

    # 5. v5/search JSON
    try:
        search = client.probe_search_api(
            makes_models="RENAULT::ZOE",
            price_max=8000,
            version="intens",
            zip_code="27000",
        )
        body = search.get("body") or {}
        hits = body.get("hits") if isinstance(body, dict) else None
        hit_count = len(hits) if isinstance(hits, list) else 0
        results["search_api"] = {"status": search.get("status"), "hits": hit_count, "total": body.get("total")}
        print(f"[5] v5/search: status={search.get('status')} hits={hit_count} total={body.get('total')}")
        results["steps"].append({"step": "search_api", "ok": hit_count > 0})
    except Exception as exc:
        print(f"[5] v5/search: FAIL — {exc}")
        results["steps"].append({"step": "search_api", "ok": False, "error": str(exc)})

    # 6. SSR listing page
    try:
        ssr = client.probe_listing_ssr(SAMPLE_LISTING_URL)
        markers = ssr.get("markers") or []
        refs = ssr.get("refs") or []
        results["ssr"] = {"status": ssr.get("status"), "markers": markers, "refs": len(refs)}
        print(f"[6] SSR listing: status={ssr.get('status')} markers={markers} refs={len(refs)}")
        results["steps"].append({"step": "ssr", "ok": len(refs) > 0 or bool(markers)})
    except Exception as exc:
        print(f"[6] SSR listing: FAIL — {exc}")
        results["steps"].append({"step": "ssr", "ok": False, "error": str(exc)})

    # 7. Distance buckets from JS
    try:
        buckets = client.probe_distance_buckets()
        results["distance_buckets"] = buckets
        print(f"[7] distance buckets from JS: {buckets}")
        results["steps"].append({"step": "distance_buckets", "ok": bool(buckets)})
    except Exception as exc:
        print(f"[7] distance buckets: FAIL — {exc}")
        results["steps"].append({"step": "distance_buckets", "ok": False, "error": str(exc)})

    # 8. Full client search (dual-strategy)
    try:
        data = client.search(make="RENAULT", model="ZOE", version="intens", price_max=8000, zip="27000", distance_km=5, limit=5)
        items = data.get("items") or []
        results["client_search"] = {
            "items": len(items),
            "source": data.get("source"),
            "total": data.get("pagination", {}).get("total"),
        }
        print(f"[8] client.search(): items={len(items)} source={data.get('source')}")
        results["steps"].append({"step": "client_search", "ok": len(items) > 0})
    except Exception as exc:
        print(f"[8] client.search(): FAIL — {exc}")
        results["steps"].append({"step": "client_search", "ok": False, "error": str(exc)})

    out = ROOT / "data" / "probe_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nResults written to {out}")

    steps = results.get("steps") or []
    ok_steps = [s for s in steps if isinstance(s, dict) and s.get("ok")]
    print(f"\n=== Summary: {len(ok_steps)}/{len(steps)} steps OK ===")

    if any(isinstance(s, dict) and s.get("step") == "client_search" and s.get("ok") for s in steps):
        print("GATE PASSED — at least one path returns listing data.")
        sys.exit(0)
    print("GATE FAILED — no path returned listing hits yet.")
    sys.exit(1)


if __name__ == "__main__":
    main()
