# Batch Specification — Lahore Food Outreach

Implementation spec for the scraper. Batches are **mutually exclusive**: every lead resolves to exactly one batch via a cascade, so a lead is never messaged twice.

---

## 0. Implementation status (added 2026-08-13)

**Built.** The cascade is `backend/src/leadscraper/core/batches.py` (pure), applied
in `services/results.py` so that the table, the CSV and the extraction clipboard
all see the same batch — §12.2's one-query rule. The batch is the results
screen's **major filter**, replacing the has-a-website split it contains.

**Verified against live data.** `scripts/spike_batches.py` re-runs the split over
every run in the database. On Lahore × food it reproduces §4's table exactly:
51 / 78 / 22 / 36 / 30 / 91 / 120, **308 sendable of 428**. The partition is
asserted exhaustive rather than eyeballed (`test_the_batches_partition_the_run_exhaustively`).

**Scope: `food` only.** Every threshold here was calibrated on one Lahore × food
scrape, and the two heaviest branches are food-shaped — `DINE_IN_SUBCATEGORIES`
is a list of restaurant subcategories, and the delivery split exists because of a
Foodpanda commission. Businesses in the other six §4 verticals resolve to
`unbatched`, which is **not a batch**: no message, no send priority, never a send
target. Measured today, the six salon runs in the database are 100% `unbatched`
(410 rows); routing them through this cascade instead would have put all 410 in
`delivery-*` and pitched delivery commission at hair salons. Defining another
vertical is the §8 exercise — measure that category's review-count percentiles
and its own equivalent of the dine-in list — not a matter of widening the
category list.

**Three departures from the prose below**, each pinned by a test in
`backend/tests/test_batches.py`:

| § | Spec says | Built as | Why |
|---|---|---|---|
| §2 | scan `phone_1`…`phone_4` | scan every ranked phone | §12.1's 4 slots are a column cap; §10.1 forbids it becoming a data cap. A business whose 5th number is WhatsApp-capable would be `B00` while the clipboard still handed that number out. |
| §2 | `wa_confidence` from the pick score | §9.3's label (`confirmed`/`likely`) | The 1–4 score only ranks two qualifying numbers against each other. Exporting it beside a phone number invites reading it as a confidence percentage. |
| §2 | `clean_num` repairs Excel escaping | kept as a guard | Its input was a CSV cell (`="+923005326559"`); ours is `contacts.value_e164`, already E.164 from §9.1. It still rejects anything under 10 characters. |

`send_rank` (§6) is computed per batch over the *current view* and shown in the
table; it does not reorder anything, because the pull is the table. §7's
per-batch CSV files are **not** built — the CSV is §12.1's 41 columns, filtered
to one batch.

---

## 1. Constants

```python
DINE_IN_SUBCATEGORIES = {
    'Art cafe', 'Bakery', 'Bar', 'Buffet restaurant', 'Cafe', 'Coffee shop',
    'Dessert restaurant', 'Dessert shop', 'Hookah bar', 'Ice cream shop', 'Steak house'
}
# Everything else in the food category is treated as delivery-capable.

VOLUME_THRESHOLD = 200    # review_count below this => early stage
RATING_THRESHOLD = 4.0    # rating below this => reputation track
WA_VALID_STATUSES = ('confirmed', 'likely')
```

---

## 2. Derived fields (compute before batching)

### `wa_number` / `wa_confidence`
Scan `phone_1` … `phone_4`. A phone qualifies if `phone_N_whatsapp` is `confirmed` or `likely`. Score each qualifying phone and keep the highest:

| Signal | Points |
|---|---|
| `phone_N_whatsapp == 'confirmed'` | +2 |
| `phone_N_whatsapp == 'likely'` | +1 |
| `phone_N_type == 'mobile'` | +1 |
| `phone_N_belongs_to == 'business'` | +1 |

**Number cleaning is mandatory.** Source values are Excel-escaped as `="+923005326559"`. Strip everything except digits and `+`; reject anything shorter than 10 characters. Skipping this breaks every send.

```python
def clean_num(v):
    s = re.sub(r'[^0-9+]', '', str(v))
    return s if len(s) >= 10 else None
```

### `is_dine_in`
`subcategory in DINE_IN_SUBCATEGORIES`

### `has_site`
`website` is non-null and non-empty.

---

## 3. Assignment cascade

Order matters. First match wins — do not reorder.

```
1. wa_number is null                      -> B00_NO_WHATSAPP
2. review_count is null or < 200          -> B05_EARLY_STAGE
3. rating < 4.0                           -> B06_REPUTATION
4. is_dine_in and has_site                -> B04_CAFE_SITE
5. is_dine_in and not has_site            -> B03_CAFE_NOSITE
6. has_site                               -> B02_DELIVERY_SITE
7. else                                   -> B01_DELIVERY_NOSITE
```

Null handling: null `review_count` (21 rows) falls to B05 — unknown volume is treated as low volume. Null `rating` (1 row) skips step 3 and continues.

---

## 4. Batch definitions

| ID | Display name | Slug | Definition | Leads | Send priority |
|---|---|---|---|---|---|
| **B01** | Commission Escape | `delivery-nosite` | Delivery-capable · 200+ reviews · rating ≥4.0 · no website | 51 | 1 |
| **B02** | Ordering Layer | `delivery-site` | Delivery-capable · 200+ reviews · rating ≥4.0 · has website | 78 | 2 |
| **B03** | Café First Presence | `cafe-nosite` | Dine-in/dessert · 200+ reviews · rating ≥4.0 · no website | 22 | 3 |
| **B04** | Café Content & Booking | `cafe-site` | Dine-in/dessert · 200+ reviews · rating ≥4.0 · has website | 36 | 4 |
| **B06** | Feedback Loop | `reputation` | 200+ reviews · rating <4.0 · any type | 30 | 5 |
| **B05** | Starter Setup | `early-stage` | <200 reviews or unknown · any type | 91 | 6 |
| **B00** | Unreachable — Email/Visit | `no-whatsapp` | No WhatsApp-capable number | 120 | — (do not send) |

Sendable total: **308**. Grand total: **428**.

---

## 5. Batch overviews

**B01 — Commission Escape (51)**
Established delivery restaurants with no web presence at all. Median 882 reviews. Highest intent in the file: proven order volume, zero owned infrastructure, and a quantifiable pain (25–35% Foodpanda commission). Offer is a WhatsApp + ordering-page system that recovers the commission on repeat customers. Roman Urdu converts better here.

**B02 — Ordering Layer (78)**
Largest and most established segment, median 1,602 reviews. They already bought a website once, which proves budget and willingness to spend on digital — the site just doesn't take orders. Pitch is an addition, never a rebuild; suggesting their site is bad kills the thread. Biggest average deal size in the file.

**B03 — Café First Presence (22)**
Cafés, coffee shops, bakeries and dessert spots with real footfall but nothing online. Smallest batch. Commission framing does not apply — these are dine-in, brand-led businesses. Offer is a one-page menu/photos/location site with a WhatsApp button for orders and table bookings.

**B04 — Café Content & Booking (36)**
Cafés that already have a site, median 1,315 reviews. Their gap is conversion and freshness, not existence: weak booking flow and stale photography while Instagram absorbs attention. Best retainer candidates in the file — content plus photography rather than a one-off build.

**B06 — Feedback Loop (30)**
Real volume (median 699 reviews) but rating below 4.0. Never reference the rating; it reads as an insult and ends the conversation. Sell the operational fix instead — a post-order WhatsApp feedback flow that intercepts complaints before Google, bundled with direct ordering.

**B05 — Starter Setup (91)**
Under 200 reviews, or volume unknown. Median 70 reviews. Thin budgets and high failure rate, so the honest recommendation is *against* a full website: Google listing, menu online, WhatsApp ordering link for the Instagram bio. Lowest priority — work it only after the first five batches are exhausted.

**B00 — Unreachable (120) — suppressed**
54 UAN, 45 landline, 21 missing. Highest median reviews of any batch at 1,407, because UAN lines signal established multi-branch operators. Do not delete. Route to email (68 available), Instagram DM, or an in-person visit.

---

## 6. Reference implementation

```python
def assign_batch(row):
    if not row['wa_number']:
        return 'B00_NO_WHATSAPP'
    rc = row['review_count']
    if rc is None or rc < VOLUME_THRESHOLD:
        return 'B05_EARLY_STAGE'
    rt = row['rating']
    if rt is not None and rt < RATING_THRESHOLD:
        return 'B06_REPUTATION'
    dine_in = row['subcategory'] in DINE_IN_SUBCATEGORIES
    has_site = bool(row.get('website'))
    if dine_in:
        return 'B04_CAFE_SITE' if has_site else 'B03_CAFE_NOSITE'
    return 'B02_DELIVERY_SITE' if has_site else 'B01_DELIVERY_NOSITE'
```

Within each batch, sort by `review_count` descending and write `send_rank`. Highest-value prospects get contacted while the number is freshest and least likely to be throttled.

---

## 7. Export contract

Each batch writes one CSV with these columns, in this order:

```
batch, send_rank, business_name, subcategory, area, wa_number, wa_confidence,
rating, review_count, website, instagram_url, facebook_url, email,
address, maps_url, contact_person
```

Plus `_batch_summary.csv`: `batch, leads, median_reviews, median_rating, file`.

---

## 8. Portability to other cities

The thresholds are calibrated to this Lahore dataset (median 736 reviews). For a smaller city, 200 and 1,500 will over-fill B05 — recalculate them as the 25th and 75th percentile of `review_count` for that scrape rather than hard-coding. `DINE_IN_SUBCATEGORIES` and the cascade order carry over unchanged.
