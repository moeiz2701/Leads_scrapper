"""Recon spike — what do §5.3's horizontal directories actually serve? (Phase 6)

§5.3's URL patterns and field notes are pre-build research. The §5.1 recon spike
found the doc wrong about where Maps keeps its phone numbers, so nothing here is
assumed: this script fetches, reports status/size/protection, and dumps bodies
into the scratchpad so the parsers can be written against real HTML.

Answers, per site:
  * does a plain fetch work at all, or is it Cloudflare/JS-walled?
  * does the URL pattern §5.3 publishes still resolve?
  * are phones in the served HTML, or fetched on demand (§5.5 mechanism B)?
  * is there a lat/lng anywhere — §10.1's fuzzy tier needs it (150 m test)

Usage:  uv run python scripts/spike_directories.py [--out DIR]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# §5.3's published patterns, plus the obvious variants, because a doc-published
# pattern that 404s is a finding rather than the end of the recon.
TARGETS: list[tuple[str, str]] = [
    ("businesslist_root", "https://www.businesslist.pk/"),
    ("businesslist_cat_doc", "https://www.businesslist.pk/category/restaurants/city:lahore"),
    ("businesslist_cat_alt", "https://www.businesslist.pk/category/restaurants"),
    ("businesslist_cat_salon", "https://www.businesslist.pk/category/beauty-salons/city:lahore"),
    ("businesslist_search", "https://www.businesslist.pk/search?q=restaurant&where=Lahore"),
    ("urdupoint_root", "https://www.urdupoint.com/business/"),
    ("urdupoint_lahore", "https://www.urdupoint.com/business/lahore/"),
    ("urdupoint_dir", "https://www.urdupoint.com/business/directory/"),
    ("businessbook_root", "https://www.businessbook.pk/"),
    ("businessbook_cat", "https://www.businessbook.pk/category/restaurants-1"),
]

# Signals worth grepping for in whatever comes back.
PHONE_RE = re.compile(r"(?:\+?92|0)[\s\-.]?3\d{2}[\s\-.]?\d{6,8}")
LANDLINE_RE = re.compile(r"(?:\+?92[\s\-.]?)?0?(?:21|42|51|41|61|91|55|52|22|81)[\s\-.]?\d{6,9}")
LATLNG_RE = re.compile(r"(?:latitude|lat)[\"'\s:=]+(-?\d{1,2}\.\d{3,})", re.I)
LNG_RE = re.compile(r"(?:longitude|lng|lon)[\"'\s:=]+(-?\d{1,3}\.\d{3,})", re.I)
CF_RE = re.compile(r"cf-browser-verification|challenge-platform|__cf_chl|cdn-cgi/l/email", re.I)
NEXT_RE = re.compile(r"__NEXT_DATA__|meta-next-head-count", re.I)


def probe(client: httpx.Client, name: str, url: str, out: Path) -> dict:
    row: dict = {"name": name, "url": url}
    try:
        response = client.get(url)
    except Exception as exc:  # noqa: BLE001 — a dead host is a finding
        row["status"] = "ERR"
        row["note"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        return row

    body = response.content
    text = body.decode("utf-8", errors="replace")
    row["status"] = response.status_code
    row["bytes"] = len(body)
    row["final"] = str(response.url) if str(response.url) != url else ""
    row["mobiles"] = len(set(PHONE_RE.findall(text)))
    row["landlines"] = len(set(LANDLINE_RE.findall(text)))
    row["lat"] = len(LATLNG_RE.findall(text))
    row["lng"] = len(LNG_RE.findall(text))
    row["cloudflare"] = bool(CF_RE.search(text))
    row["nextjs"] = bool(NEXT_RE.search(text))

    path = out / f"{name}.html"
    path.write_bytes(body)
    row["saved"] = path.name
    return row


def check_categories(city: str = "Lahore") -> int:
    """Open every mapped BusinessList category and print what is actually in it.

    BusinessList's categories are self-assigned by the businesses that submit
    the listing, so a slug's *name* is not evidence of its contents — mapping
    ``food-retailers`` to `food` pulled in 854 groceries and B2B wholesalers.
    Re-run this whenever ``CATEGORY_SLUGS`` is touched, and read the sample
    names rather than the totals.
    """
    import time

    from leadscraper.sources.businesslist import CATEGORY_SLUGS, parse_listing_page

    with httpx.Client(
        follow_redirects=True, timeout=25.0, verify=False, headers={"User-Agent": USER_AGENT}
    ) as client:
        for category, slugs in CATEGORY_SLUGS.items():
            print(f"### {category.value}")
            for slug in slugs:
                time.sleep(1.0)
                url = f"https://www.businesslist.pk/category/{slug}/city:{city.lower()}"
                try:
                    response = client.get(url)
                except Exception as exc:  # noqa: BLE001
                    print(f"   {slug:28} ERR {type(exc).__name__}")
                    continue
                if response.status_code != 200:
                    print(f"   {slug:28} HTTP {response.status_code}")
                    continue
                page = parse_listing_page(url, response.content)
                sample = [x.name[:22] for x in page.listings[:4]]
                phones = sum(1 for x in page.listings if x.phones)
                coords = sum(1 for x in page.listings if x.has_coordinates)
                print(
                    f"   {slug:28} total={str(page.total_found):>5} "
                    f"page={len(page.listings):>2} ph={phones:>2} geo={coords:>2}  {sample}"
                )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="./data/spike/directories")
    parser.add_argument(
        "--categories",
        action="store_true",
        help="audit what each mapped BusinessList category actually contains",
    )
    args = parser.parse_args()

    if args.categories:
        return check_categories()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with httpx.Client(
        follow_redirects=True,
        timeout=25.0,
        verify=False,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-PK,en;q=0.9,ur;q=0.8",
        },
    ) as client:
        rows = [probe(client, name, url, out) for name, url in TARGETS]

    header = (
        f"{'name':26} {'stat':>5} {'bytes':>8} {'mob':>4} {'land':>5} "
        f"{'lat':>4} {'lng':>4}  flags"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        flags = []
        if row.get("cloudflare"):
            flags.append("CLOUDFLARE")
        if row.get("nextjs"):
            flags.append("NEXTJS")
        if row.get("final"):
            flags.append(f"->{row['final'][:60]}")
        if row.get("note"):
            flags.append(row["note"])
        print(
            f"{row['name']:26} {str(row['status']):>5} {row.get('bytes', 0):>8} "
            f"{row.get('mobiles', 0):>4} {row.get('landlines', 0):>5} "
            f"{row.get('lat', 0):>4} {row.get('lng', 0):>4}  {' '.join(flags)}"
        )

    print(f"\nBodies in {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
