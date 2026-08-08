"""Recon spike: is the phone number already in the Maps *search* payload?

Why this exists
---------------
implementation.md §5.1 says "the results list does not show phone numbers. You
must open each place panel — budget one interaction per business", and §14 costs
that at 700 interactions / 28 minutes for one Lahore × salon run.

That claim is true of the *rendered DOM*. §5.1 also tells us to read the network
response instead of the DOM — and the search response is a different object from
the rendered list. If phones are in it, Stage 2 for Maps collapses from ~700
interactions to ~60 payload parses and the run drops from ~57 min to ~20.

This script answers that question with evidence rather than assumption. It
captures the raw payloads to disk first, so the analysis can be re-run and
argued with later without touching Google again.

Usage
-----
    uv run python scripts/spike_maps_payload.py                    # capture + analyse
    uv run python scripts/spike_maps_payload.py --headed           # watch it
    uv run python scripts/spike_maps_payload.py --analyse-only     # reuse saved payloads

Note on geography: without a PK residential proxy the *results* will be
geo-ranked for the wrong country (§7.1). That does not affect what this spike
measures — we are after the shape of the payload, not the identity of the
businesses in it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from leadscraper.core.phone import extract_phones

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "spike_maps"

# One query per city area. Deliberately tiny — this is recon, not a run.
QUERIES = [
    "salon in Gulberg, Lahore",
    "restaurant in DHA, Lahore",
]

# The search-results transport. Maps issues this as an XHR; it is not the page.
SEARCH_URL_MARKERS = ("/search?tbm=map", "tbm=map")
PLACE_URL_MARKERS = ("/maps/preview/place", "/maps/rpc/", "/maps/preview/")

JSON_GUARD_RE = re.compile(r"^\)\]\}'\s*")


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #


async def capture(headed: bool) -> list[Path]:
    from playwright.async_api import async_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    seen = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        context = await browser.new_context(
            locale="en-GB",
            timezone_id="Asia/Karachi",
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        async def on_response(response) -> None:
            nonlocal seen
            url = response.url
            is_search = any(m in url for m in SEARCH_URL_MARKERS)
            is_place = any(m in url for m in PLACE_URL_MARKERS)
            if not (is_search or is_place):
                return
            try:
                body = await response.body()
            except Exception:  # noqa: BLE001 — response already consumed/aborted
                return
            if len(body) < 1000:
                return
            seen += 1
            kind = "search" if is_search else "place"
            path = OUT_DIR / f"{kind}_{seen:03d}.txt"
            path.write_bytes(body)
            saved.append(path)
            print(f"  captured {kind:6} {len(body):>9,} bytes  -> {path.name}")

        page.on("response", on_response)

        for query in QUERIES:
            print(f"\n[query] {query}")
            url = "https://www.google.com/maps/search/" + query.replace(" ", "+")
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await _dismiss_consent(page)
            # Let the results list settle and lazy-load a second page of results.
            await page.wait_for_timeout(6_000)
            try:
                await page.mouse.wheel(0, 3000)
                await page.wait_for_timeout(4_000)
            except Exception:  # noqa: BLE001
                pass

        await context.close()
        await browser.close()

    return saved


async def _dismiss_consent(page) -> None:
    """Google's cookie interstitial. Public, pre-auth, one click — not a wall."""
    for selector in (
        'button:has-text("Accept all")',
        'button:has-text("Reject all")',
        'form[action*="consent"] button',
    ):
        try:
            button = page.locator(selector).first
            if await button.count() and await button.is_visible():
                await button.click(timeout=5_000)
                await page.wait_for_timeout(2_000)
                return
        except Exception:  # noqa: BLE001
            continue


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #


def parse_payload(raw: bytes) -> Any | None:
    text = raw.decode("utf-8", errors="replace")
    text = JSON_GUARD_RE.sub("", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def walk_strings(node: Any, path: tuple = ()):
    """Yield (path, string) for every string in a positional nested array.

    Maps payloads have no keys — position *is* the schema — so recording the
    index path is the only way to turn a finding into a parser later.
    """
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, list):
        for i, child in enumerate(node):
            yield from walk_strings(child, (*path, i))
    elif isinstance(node, dict):
        for k, child in node.items():
            yield from walk_strings(child, (*path, k))


def analyse(paths: list[Path]) -> dict[str, Any]:
    findings: dict[str, Any] = {"search": [], "place": []}

    for path in sorted(paths):
        kind = "search" if path.name.startswith("search") else "place"
        raw = path.read_bytes()
        parsed = parse_payload(raw)

        # Even if JSON parsing fails, a raw-text scan tells us whether the digits
        # are present at all — that alone answers the costing question.
        text = raw.decode("utf-8", errors="replace")
        raw_hits = extract_phones(text)

        structured_hits: list[dict] = []
        if parsed is not None:
            for node_path, value in walk_strings(parsed):
                for phone in extract_phones(value):
                    structured_hits.append(
                        {"path": list(node_path), "raw": value.strip()[:60], "e164": phone.e164}
                    )

        findings[kind].append(
            {
                "file": path.name,
                "bytes": len(raw),
                "json_parsed": parsed is not None,
                "raw_text_phone_count": len({p.e164 for p in raw_hits}),
                "structured_phone_count": len({h["e164"] for h in structured_hits}),
                "sample_paths": structured_hits[:8],
                "sample_e164": sorted({p.e164 for p in raw_hits})[:10],
            }
        )

    return findings


def report(findings: dict[str, Any]) -> None:
    print("\n" + "=" * 78)
    print("MAPS PAYLOAD RECON — does the search response already carry phones?")
    print("=" * 78)

    for kind in ("search", "place"):
        entries = findings[kind]
        print(f"\n--- {kind.upper()} responses: {len(entries)} captured ---")
        for entry in entries:
            print(
                f"  {entry['file']:<16} {entry['bytes']:>9,}B  "
                f"json={'yes' if entry['json_parsed'] else 'NO':<3}  "
                f"phones(raw)={entry['raw_text_phone_count']:<4} "
                f"phones(structured)={entry['structured_phone_count']}"
            )
            if entry["sample_e164"]:
                print(f"      numbers: {', '.join(entry['sample_e164'][:6])}")
            for hit in entry["sample_paths"][:3]:
                print(f"      at {hit['path']}  ->  {hit['e164']}")

    search_total = sum(e["raw_text_phone_count"] for e in findings["search"])
    print("\n" + "-" * 78)
    if search_total:
        print(
            f"VERDICT: the search payload carries {search_total} distinct numbers.\n"
            "         §5.1's 'you must open each place panel' is true of the DOM,\n"
            "         not of the network response. Stage 2 for Maps can parse the\n"
            "         list payload instead of interacting per business."
        )
    else:
        print(
            "VERDICT: no phone numbers in the search payload.\n"
            "         §5.1 and §14 stand as written — budget one detail-panel\n"
            "         interaction per business."
        )
    print("-" * 78)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--analyse-only", action="store_true")
    args = parser.parse_args()

    if args.analyse_only:
        paths = sorted(OUT_DIR.glob("*.txt"))
        if not paths:
            print(f"No saved payloads in {OUT_DIR}", file=sys.stderr)
            return 1
    else:
        paths = asyncio.run(capture(headed=args.headed))
        if not paths:
            print("Captured nothing — Maps may have served a different transport.", file=sys.stderr)
            return 1

    findings = analyse(paths)
    report(findings)
    (OUT_DIR / "findings.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
    print(f"\nFull findings: {OUT_DIR / 'findings.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
