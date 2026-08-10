# Pakistan Local Business Lead Scraper — Implementation Document

**Version 1.0 · August 2026**

Target: **300–800 contactable leads per run**, where a run = one (city × category) pair.
Primary output: a business's WhatsApp-capable number, attributed to an owner/CEO where possible.

---

## 1. Scope and non-goals

### In scope

- Discovery of local businesses by city + category across 7 verticals
- Extraction of phone numbers, WhatsApp evidence, emails, socials, addresses
- Attribution of a number to a named person (owner / CEO / director / agent) where the public record supports it
- Ranked, deduplicated output as a sortable table with CSV export

### Explicitly not goals

- Personal (non-published) phone numbers of individuals
- Any number obtained by circumventing a login wall or platform anti-bot control
- Automated WhatsApp Web contact-checking (see §9.3 — this gets your number banned and is not permitted by WhatsApp)

### Design principle

Every record carries **provenance**: which URL it came from, when, and how confident we are. This is not bureaucracy — it is what lets you re-parse when selectors break, honour a removal request, and debug a bad batch without re-scraping.

---

## 2. System architecture

Six stages, each an independent queue consumer. **Do not build this as one browser script.** Selectors break weekly; you need to re-run stage 3 without re-running stage 1.

```
┌─────────────────────────────────────────────────────────────┐
│  INPUT:  city · category · number_preference · source flags │
└───────────────────────────┬─────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ STAGE 1 · DISCOVERY          → business entities            │
│   Maps grid fan-out, vertical directories, horizontal dirs  │
│   Emits: name, address, lat/lng, website, maps_place_id     │
└───────────────────────────┬─────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ STAGE 2 · CONTACT ENRICHMENT → raw contact strings          │
│   Maps detail panel, own website, wa.me links, directories  │
└───────────────────────────┬─────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ STAGE 3 · SOCIAL ENRICHMENT  (optional, toggled)            │
│   FB Page public data, IG bio → bio-link follow             │
└───────────────────────────┬─────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ STAGE 4 · PERSON ATTRIBUTION → name + role, linked to phone │
└───────────────────────────┬─────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ STAGE 5 · NORMALISE & SCORE  → E.164, WA evidence, rank     │
└───────────────────────────┬─────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ STAGE 6 · DEDUPE & EXPORT    → table + CSV                  │
└─────────────────────────────────────────────────────────────┘
```

### Recommended stack

| Layer | Choice | Why |
|---|---|---|
| API / orchestration | FastAPI (Python) | Same language as scrapers, good async |
| Queue | Redis + RQ (or Celery) | Per-stage queues, retry, visibility |
| Browser | Playwright (Chromium, persistent context) | Network interception, click-to-reveal |
| Plain fetch | httpx + selectolax | Most directory pages need no browser — 10× faster |
| Store | PostgreSQL | Relational contact↔business model, JSONB for raw |
| Raw archive | S3 / local disk | Every fetched HTML+JSON, keyed by URL hash |
| Phone parsing | `phonenumbers` (libphonenumber port) | Region `PK` |
| Frontend | Next.js + TanStack Table | Sort/filter/column-visibility out of the box |

**Never re-fetch what you have.** A content-addressed cache on URL hash with a TTL (7 days for listings, 30 for detail pages) cuts your request volume by 60–80% across runs and is the single biggest thing that keeps you under rate limits.

---

## 3. Input specification

```jsonc
{
  "city": "Lahore",                    // enum, see §3.1
  "category": "salon",                 // enum, see §4
  "subcategories": ["barber", "spa"],  // optional narrowing
  "number_preference": "owner_first",  // owner_first | business_first | whatsapp_only
  "sources": {
    "google_maps":     true,   // core, default on
    "business_website":true,   // core, default on
    "directories":     true,   // core, default on
    "vertical":        true,   // auto-selected per category
    "facebook":        false,  // toggle
    "instagram":       false   // toggle
  },
  "target_leads": 500,
  "max_runtime_minutes": 90
}
```

### 3.1 Cities (tier 1 first)

Karachi, Lahore, Islamabad, Rawalpindi, Faisalabad, Multan, Peshawar, Gujranwala, Sialkot, Hyderabad, Quetta, Bahawalpur, Sargodha, Gujrat, Abbottabad.

Each city record carries a list of **commercial tiles** (§5.1) — this is what makes volume achievable.

### 3.2 Seed input (alternative entry point)

As well as city+category discovery, the pipeline accepts a **seed list** — skip Stage 1 and run enrichment against businesses you already have. Useful for lists bought elsewhere, exports from a CRM, or re-enriching an old run.

```jsonc
{
  "mode": "seed",
  "seed_type": "domains",     // domains | names_and_city | maps_urls | place_ids
  "seed_file": "leads.csv",
  "number_preference": "owner_first",
  "sources": { "business_website": true, "directories": true, "facebook": true }
}
```

| `seed_type` | Required CSV column | Enters at |
|---|---|---|
| `domains` | `website` | Stage 2 (website module) |
| `names_and_city` | `business_name`, `city` | Stage 1, Maps lookup per row |
| `maps_urls` / `place_ids` | `maps_url` or `place_id` | Stage 2, straight to detail panel |

Seeded rows carry `source = "seed"` and skip the grid fan-out entirely, so a 500-domain list enriches in ~15 minutes rather than an hour.

### 3.3 `number_preference` semantics

This controls **ranking**, not filtering (except `whatsapp_only`).

| Preference | Ranking order applied to a business's contact set |
|---|---|
| `owner_first` | named-person number → mobile w/ WA confirmed → mobile → landline |
| `business_first` | main business line → mobile w/ WA confirmed → named-person → landline |
| `whatsapp_only` | filter out everything without WhatsApp evidence, then rank by confidence |

---

## 4. Category taxonomy and source routing

The critical finding from source research: **a dedicated vertical directory only exists for 3 of your 7 categories.** Build the router to reflect that rather than pretending otherwise.

| Category | Vertical sources | Vertical strength | Volume driver |
|---|---|---|---|
| **food** | Foodpanda, Tossdown | Strong | Maps + Foodpanda |
| **real_estate** | Zameen, Graana, Ilaan, Aarz | Strong (needs click-reveal) | Zameen agencies |
| **entertainment** | **PakPlay, Turfy** | Strong — WhatsApp links in plain HTML | PakPlay + Maps |
| **salon** | — none | None | **Maps** |
| **car_services** | PakWheels (partner network only) | Weak | **Maps** |
| **fashion** | — none | None | **Maps + FB/IG** |
| **ecommerce** | Daraz/PriceOye (no seller phones) | None | **Maps + FB/IG** |

> **Note on KhelPoint:** its `/venues` page serves 8 placeholder listings with Unsplash stock imagery and "book via app" gating. It is a marketing site, not a data source. Do not build a module for it.

> **Note on Cheetay:** shut down completely in 2024. Do not build a module for it.

### 4.1 Sources deliberately excluded

Documented so nobody re-litigates these mid-build.

| Source | Why excluded |
|---|---|
| **LinkedIn** | Near-zero phone data (gives names/titles, not numbers), aggressively anti-bot, actively litigates, bans fast. Its only value is owner-name enrichment — not worth the risk or the build. Use the SECP register or the business's own About page instead |
| **TikTok** | Heavy JS, aggressive fingerprinting, negligible phone yield for these verticals |
| **KhelPoint** | `/venues` serves 8 placeholder listings on Unsplash stock imagery, gated behind "book via app". Marketing site, not a data source |
| **Cheetay** | Shut down completely, Jan 2024 |
| **Apollo / ZoomInfo / Hunter.io / Lusha** | Built on US/EU corporate data. Effectively no coverage of Pakistani SMBs — a Lahore salon or a Gujranwala padel court will not be in them. Do not buy a seat expecting local coverage |
| **Daraz / PriceOye seller pages** | Marketplaces don't expose seller phone numbers. Useful for discovering that a store exists, useless for contacting it |

### 4.2 Query expansion dictionary

Maps caps a single query at roughly 120 results. Volume comes from **synonym fan-out**, and in Pakistan that means including local/transliterated terms. This dictionary is the highest-leverage config file in the system.

```yaml
food:
  - restaurant / cafe / coffee shop / bakery
  - karahi / bbq / tikka shop / nihari / biryani
  - fast food / burger / pizza / broast
  - sweets / mithai / halwa puri / dhaba

fashion:
  - boutique / clothing store / garments
  - ladies tailor / darzi / stitching
  - unstitched / pret / lawn / kids wear
  - shoe store / fabric shop / cloth house

ecommerce:
  - mobile shop / electronics store / computer shop
  - home appliances / AC shop / LED shop
  - auto parts / spare parts / sports shop

real_estate:
  - property dealer / estate agency / real estate
  - builders / developers / marketing / consultant

salon:
  - salon / saloon / beauty parlour / beauty parlor
  - hair salon / barber / hajaam / men's salon
  - ladies salon / spa / massage centre / bridal studio

car_services:
  - car workshop / auto workshop / mechanic
  - denting painting / car detailing / car tuning
  - car AC / wheel alignment / service station / car wash

entertainment:
  - snooker club / pool club / gaming zone
  - indoor play area / kids play area / trampoline park
  - padel / futsal / indoor cricket / arcade / PS5 lounge
```

---

## 5. Core source modules

### 5.1 Google Maps — the volume engine (~70% of all leads)

**Discovery: grid × synonym fan-out.**

A single "salons in Lahore" query returns ≤120 results. To reach several hundred you tile the city by commercial district and multiply by synonyms.

```
queries_per_run = len(commercial_tiles) × len(synonyms[category])
```

Lahore tiles (example): DHA, Gulberg, Johar Town, Model Town, Bahria Town, Faisal Town, Garden Town, Iqbal Town, Township, Cantt, Wapda Town, Valencia.

```
12 tiles × 5 synonyms = 60 queries
60 × ~40 unique results  = ~2,400 raw
after cross-query dedupe = ~600–1,000 unique businesses
× ~85% have a listed phone = 500–850 leads  ✓
```

**Extraction: read the network response, not the DOM.**

Maps loads results through internal JSON/protobuf payloads. Intercept them with Playwright rather than scraping rendered text — far more stable across redesigns.

```python
page.on("response", handle)   # capture /search?tbm=map & /place payloads
```

Fall back to DOM parsing only when the payload shape changes.

**Note — superseded by measurement, Aug 2026.** This section previously said the results list does not show phone numbers and you must open each place panel, budgeting one interaction per business. That is true of the **rendered DOM** but not of the **network response** this same section tells you to read. The search payload already carries the phone.

Measured over 3 live queries (Islamabad × salon, 249 raw results):

| Field | Fill rate | Field | Fill rate |
|---|---|---|---|
| name / place_id | 100% | **phone** | **87%** |
| address | 100% | rating | 100% |
| lat / lng | 100% | category | 100% |
| review_count | varies — see below | **website** | **39%** |

> **Correction.** An earlier revision of this note claimed phone fill of 100%, measured over 40 results. That sample was the *first page only*. Across paginated results the true rate is **87%** — which lands almost exactly on this section's original "~85% have a listed phone". The 100% figure was an artefact of a small first-page sample, not a better source.

> **Second correction, same cause — Aug 2026.** The website fill above read ~85% for exactly the same reason, and it is **39%** across the full 199-business Islamabad run (78 of 199 carry any URL; 63 a real website, 15 a Facebook or Instagram profile that §5.2 routes to the social columns). Businesses ranked onto page 1 are the ones that have a website, so a first-page sample overstates it by more than 2×. The honest figure is close to §14's original "~30% have sites", which the Phase 3 run then confirmed. **The lesson generalises: never characterise this payload from the first page.** Two of the three fill rates measured that way were wrong, and both were wrong in the optimistic direction.

**Consequence:** Stage 2 for Maps parses search responses instead of performing ~700 detail-panel interactions. See the revised §14 timings. Detail panels drop to a *fallback* for the ~13% of records where the payload lacks a phone, not the default path.

**Two payload formats, and the trap between them.** The first page of results arrives as a bare guarded array. The pages that lazy-load as you scroll arrive **wrapped**:

```
{"c":0,"d":")]}'\n[[ ...the real array... ]", ...}/*""*/
```

— the payload JSON-encoded inside `d`, with a comment trailer, sometimes several documents concatenated in one body. A plain `json.loads` raises `Extra data` on that. If the parser swallows the error, four out of every five captured payloads silently yield zero results while the run reports success — the §5.5 failure mode, on the volume engine. Once unwrapped the inner array has the identical shape, so one field map serves both.

**Scroll to exhaustion, and measure progress by feed height, not payload arrival.** Maps lazy-loads ~20 results at a time toward its ~120-per-query cap. Pagination responses fire only every ~3 scrolls while the feed grows on every one, so an idle-check keyed on payload arrival stops after two scrolls. Combined effect of getting both of these right, on the same 3 queries:

| | Results/query | Unique businesses |
|---|---|---|
| One scroll, envelope unhandled | 20 | 60 |
| Scroll to exhaustion + envelope parsed | 54–99 | **199** |

That is a **3.3× volume increase for the same number of navigations** — by a wide margin the cheapest volume in the system, since the page is already loaded and paid for.

Reproduce with `backend/scripts/spike_maps_payload.py`; the parser and its golden-file tests are `core/maps_payload.py` and `tests/test_maps_payload.py`.

Two cautions that came out of the same spike:

- **Payload richness varies between responses from the same endpoint.** The lighter 194 KB response omitted `review_count` for all 20 results; the 828 KB one had it for all 20. Treat a missing field as missing, never as zero — §10.2 feeds `business_signal` from it and a fabricated 0 would push good leads *down* the ranking.
- **The payload is positional and unversioned.** Index 178 means "phone" only by a convention Google never promised to keep. Every access must degrade to `None` rather than raise, and the field map must be pinned by a golden-file test — otherwise a reshuffle becomes the §5.5 silent-breakage scenario on the volume engine itself.

Where an interaction is still needed, realistic throughput is **200–500 businesses/hour per browser** at humane pacing. Note that §14's timings assume roughly four browser workers in parallel, not one.

**Fields:** name, category, full address, lat/lng, phone, website, rating, review_count, place_id, price_range. `plus_code` and `hours` were not located in the search payload — treat them as detail-panel-only.

A further observation, worth an A/B test once a proxy exists: the spike ran from a **non-PK IP** and still returned correct Lahore businesses, because the query text named the area explicitly ("salon in Gulberg, Lahore"). This does not retire the §7.1 proxy requirement — ranking, completeness and result count may still differ materially — but it does suggest "returns the wrong businesses entirely" is stronger than what a location-qualified query actually does. Measure it before assuming either way.

`rating` and `review_count` are your best free proxy for business size and legitimacy — feed both into lead scoring.

---

### 5.2 Business website — the WhatsApp confirmation engine

Zero anti-bot friction, and the **only source that gives definitive WhatsApp evidence.** Most sites need no browser at all — plain `httpx` + parser.

Crawl budget: homepage, `/contact*`, `/about*`, footer of any page. Max 4 pages per domain.

Extract, in priority order:

1. `a[href^="https://wa.me/"]` and `a[href*="api.whatsapp.com/send"]` → **confirmed WhatsApp**, highest confidence in the entire system
2. Floating WhatsApp chat widgets (`.wa-widget`, `#whatsapp-button`, Tawk/Elfsight embeds) → parse `data-phone`
3. `a[href^="tel:"]` → phone, medium confidence
4. Regex over visible text (§9.1) → phone, lower confidence
5. `a[href^="mailto:"]`, plus social profile links → feeds Stage 3
6. JSON-LD `LocalBusiness` / `Organization` blocks → `telephone`, `founder`, `employee` (occasionally yields owner name)

**Note — measured, Aug 2026 (Phase 3).** Built and run against the discovered businesses of two live Maps runs. Modules: `core/webparse.py` (page → fields), `core/site_evidence.py` (pages → §9.3-scored findings), `sources/website.py` (fetch/cache/budget), `services/enrichment.py` (Stage 2 body).

| | Islamabad × salon | Lahore × salon |
|---|---|---|
| Businesses discovered | 199 | 60 |
| With a website (§5.1 payload) | 63 → **62 domains** | 36 → **31 domains** |
| Pages fetched | 91 (**1.5/domain**) | 26 |
| Domains yielding something | 47 | 19 |
| Domains dead (DNS/timeout/404) | 13 (21%) | 7 (23%) |
| Domains refusing (403/429) | 0 | 2 |
| New phone contacts | 82 | 50 |
| Emails | 41 | 24 |
| **Confirmed WhatsApp numbers** | **53** | **5** |
| Businesses with ≥1 confirmed | 28 (45% of crawled domains) | 4 (13%) |
| Owner names (JSON-LD `founder`) | 2 | 0 |

Re-running the Islamabad pass inside the cache TTL took **13 seconds against ~5 minutes live**, made 0 requests and changed 0 rows — the §7 lever and the §10.1 "never discard, only fill gaps" merge, both visible in one command. Note the corollary: the §7 delay must be spent only behind a request that was actually made. Pacing after a cache hit would put the five minutes straight back.

Four things worth acting on:

- **This section's name is half right.** Of the 53 confirmed numbers, only **19 were confirmations of a number Maps already had** — the other **34 are numbers Maps never carried at all**. Put the other way: a business's own site confirmed just **15% of the Maps mobiles** (19 of 127), because the number a salon publishes as its WhatsApp is usually *not* the number it registered on its Maps listing. So §5.2 is a confirmation engine *and* a discovery source, and the second job is the bigger one. Do not size it as an enrichment-only pass.
- **The 4-page budget is not the binding constraint — 1.5 pages per domain is.** The crawl stops as soon as it has a confirmed number and an email, and most sites give both on the homepage. The budget is a ceiling that is rarely reached, so raising it would buy very little; the yield lever is the 39% of businesses that have a site at all, not the depth of the crawl.
- **Yield varies enormously by city/category slice** — 45% of crawled Islamabad domains produced a confirmed number against 13% in Lahore, on the same code and the same category. The Islamabad salon set skews to clinics and spas with real websites. **Measure per slice; do not extrapolate one run's confirmation rate into the §13 "estimated available" figure.**
- **Roughly one domain in five is dead** (21% / 23%). That is a normal, permanent property of PK SMB hosting, not a fault to chase, and it must not be allowed to look like a failure — see the breaker note below.

**Two implementation traps, both found by running it.**

*One host refusing is not the source refusing.* §7's circuit breaker is specified per **source**, which is right for Maps — Maps is one source, and a 429 from Google means every later query is futile and rude. "Business websites" is not one source; it is a few hundred unrelated hosts. On the first live Lahore run a **single 403 from one salon's WAF tripped a source-level breaker and skipped 19 healthy domains**, turning a 61%-yield run into a 23% one. That is §7's "continue the run with the remaining sources" inverted. The rule this source needs:

- A refusal (403/429/503) abandons **that domain**, immediately and without retry, and is recorded as a per-record outcome. It does not degrade the run.
- A refusal **streak across 10 consecutive domains** is a different fact — that is our egress being blocked rather than one strict host — and *does* trip the source breaker.
- The §5.5 empty-success check still applies, but at a threshold of 25 domains rather than 5. A real fraction of these sites are a splash image with no contact details, so a threshold of 5 false-trips about once every thousand domains; a genuine extractor break yields 0% and trips at 25 regardless.

*A redirect splits the archive key from the evidence URL.* About **one domain in nine redirects http→https**, and the contact records the *final* URL as its WhatsApp evidence while the fetch is keyed on the *requested* one. Archive under only the requested URL and §2's re-parse path cannot find the page that proved the number; archive under only the final URL and the next run's cache lookup misses and re-fetches. Store the body under both — 7 of the first 53 confirmed rows were unresolvable against the archive until this was fixed.

**Contact confidence does not follow the 1–6 order above.** That list ranks *WhatsApp* evidence, which is why JSON-LD sits last. For §10.2's `contact_confidence` — "is this really the business's number" — a machine-readable `telephone` inside a structured record beats a free-text digit run, so the ladder used is wa.me/widget 0.95 → JSON-LD 0.90 → `tel:` 0.85 → regex text 0.60. Two rankings, two different questions, both applied.

**Not implemented, deliberately: `robots.txt`.** This source fetches at most 4 pages of a business's own public contact pages, once per run, at 1–3s per host. A default WordPress `robots.txt` permits it and a misconfigured one — common on this hosting — would zero out the module, which the §5.5 convention says is the outcome to avoid above all others. Revisit if a real complaint ever arrives.

**Observed and left for Phase 8:** Maps sometimes returns a `linktr.ee/...` URL in the website field. That is a §6.4 bio-link, not a §5.2 website, and it should be routed to the bio-link follower rather than crawled as a site.

---

### 5.3 Horizontal directories

| Site | Access | URL pattern | Notes |
|---|---|---|---|
| **BusinessList.pk** | plain fetch, no auth | `/category/{cat}/city:{city}`, `/category/{cat}/{page}` | Phones in plain text. 155k companies. Emails paywalled |
| **UrduPoint Directory** | plain fetch | `/business/directory/{id}/{slug}.html` → `/detail/` | Clean field table (`Mob #`). Mostly `03xx`. Emails Cloudflare-obfuscated. Has an owner-name field, **sparsely populated** |
| **BusinessBook.pk** | JS-rendered | `/category/{slug}-{id}` | Requires browser |
| **YellowPage.pk / listing.com.pk** | plain fetch | varies | Low quality, high duplication — use as corroboration only |

**Coverage warning — set expectations correctly.** BusinessList.pk is broad but shallow: 346 restaurants nationally, but only **18 beauty salons in Lahore**. These directories are a corroboration and cross-check layer, **not** a volume driver. Google Maps carries the run.

---

### 5.4 Vertical modules

**PakPlay** (`pakplay.co`) — best non-Maps source in the whole system. Server-rendered venue pages, no login, containing both `tel:` and an explicit `wa.me` link:

```
/venue/{slug} → 03000516608 → wa.me/923000516608
```

504 venues, 16 cities. Index at `/venues?sport={cricket|futsal|padel}`. The page also embeds the Google Maps place ID inside the map iframe URL (`!1s0x38df97d8b1451399:0x9d9fcf8d9ac7790f`) — extract it as a free join key to your Maps records.

**Turfy** (`goturfy.com`) — same model. 45 venues, `/venue/{slug}`, `/venues?city=`. Its entire booking flow is "message the venue on WhatsApp", so venue pages carry the number by design.

**Zameen** — `/agents/{City}-1/` paginates 1,525 Lahore agencies over 153 pages. Agency name, description, city and listing counts are in server HTML; **the phone sits behind a "Call" button and requires a click-to-reveal in a browser.** Budget one interaction per agency. Agency detail pages list named staff — the best owner-attribution source in the system (§8).

**Foodpanda** — vendor listing pages behind a JSON endpoint. Gives name, address, cuisine; phone rarely. Use for discovery, then hand off to Maps/website for contact.

---

### 5.5 Handling click-to-reveal interactions

Zameen's "Call" button, Maps' place panel and similar patterns all need an interaction. Before writing any clicking code, **diagnose which mechanism you're facing** — in two of the three cases you never click at all in production.

#### Step 1 — Diagnose (once, manually, per site)

Open one page in Playwright with devtools. Click the reveal button once. Then check, in order:

| Mechanism | How to detect | Production approach |
|---|---|---|
| **A. Already in payload** | Number appears in initial HTML source, a `data-` attribute, or an embedded JSON blob — merely hidden by CSS | **Never click.** Parse the payload |
| **B. Fetched on demand** | Network tab shows an XHR fired by the click, returning JSON | **Replay the endpoint directly** |
| **C. Rendered as image** | Reveals an `<img>`, no text in DOM | OCR, or skip the record |

**Zameen is a Next.js app** (confirmed: `meta-next-head-count` in the served HTML). Next.js embeds the page's props as JSON in a `__NEXT_DATA__` script tag in the server response. Agent contact fields are frequently present there even when the UI hides them behind a button. So check mechanism A first:

```python
import json
blob = json.loads(page.locator("#__NEXT_DATA__").inner_text())
# walk blob["props"]["pageProps"] for phone/mobile/contact keys
```

If that's a hit, Zameen becomes a plain-fetch source and the click-to-reveal cost disappears entirely.

#### Step 2 — If mechanism B, capture the contract

```python
async with page.expect_response(
    lambda r: "/phone" in r.url or "/contact" in r.url
) as resp_info:
    await page.click("button:has-text('Call')")

resp = await resp_info.value
print(resp.url, resp.request.headers, await resp.json())
```

Record the URL template, required headers and auth. Then **stop using the browser for this source.**

#### Step 3 — Replay the endpoint at scale

This is the whole point of the exercise. One browser session discovers the contract; thousands of cheap `httpx` calls consume it.

```python
# Bootstrap session once in Playwright, export cookies
cookies = {c["name"]: c["value"] for c in context.cookies()}

# Then for all 1,525 Lahore agencies:
r = await client.get(
    f"https://www.zameen.com/api/agents/{agent_id}/phone",
    cookies=cookies,
    headers={"Referer": agency_url, "X-Requested-With": "XMLHttpRequest"},
)
```

Throughput difference is large enough to change your planning:

| Approach | Zameen agencies/hour |
|---|---|
| Browser click per agency | 300–500 |
| Replayed API endpoint | 3,000–5,000 |

Session cookies expire. On a `401`/`403`, re-bootstrap through Playwright once and resume — don't fail the run.

#### Step 4 — Fallback ladder

Try in order, per record: payload parse → API replay → browser click → mark `reveal_failed`.

#### Interaction failure handling

- Wrap every click in a 10s timeout. On timeout, screenshot, mark the contact row `reveal_failed`, **continue the run.** One stubborn record must never stall a queue.
- Retry `reveal_failed` rows once on the next run, not immediately — transient UI states usually resolve by then.
- Selector drift is the top cause of silent breakage. Assert a non-empty, phone-shaped result after each reveal; if 5 consecutive reveals come back empty, circuit-break the source and alert. Otherwise you'll harvest 1,500 blank rows and not notice.
- Interactions are idempotent by URL — cache the revealed number keyed on the record URL so a re-run never re-clicks.

#### Where this approach does *not* apply

A reveal button on public data and a login wall are different things. Zameen shows that number to any anonymous visitor who clicks — the button is a UI affordance and usually a lazy-load, so clicking it is ordinary browsing. Facebook's login modal is an access control. The §6 design contains **no interaction handling for FB/IG at all**, deliberately:

- Tier 3 reads `og:description` from static meta tags on a single logged-out page load — nothing to click
- If a number sits behind a wall, the record is marked `blocked` and routed to SERP discovery (Tier 2) or the operator queue (Tier 4)

That's the line: automate interactions with public data, never automate past an access control.

---

## 6. Facebook & Instagram module (toggleable)

This is where your fashion and ecommerce leads live, so it deserves a real design rather than a bolt-on.

### 6.1 A necessary reframe

You asked for extraction that stays "silent" and avoids the walls. I'm not going to spec anti-detection evasion — account rotation, fingerprint spoofing, CAPTCHA solving. That's circumvention of platform access controls, it breaks every few weeks, and it poisons your dataset with half-scraped records from banned sessions.

**More usefully: you don't need it.** For Pakistani fashion and ecommerce SMBs the phone number is almost never on the Instagram profile itself — it's *one hop away*, on a page with no wall at all. The four tiers below get you the same data, more reliably, and they keep working.

The genuine "quiet" lever isn't disguise. It's **volume**: aggressive caching, one page-load per business, honest backoff. A crawler that makes 200 requests an hour doesn't need to hide.

### 6.2 Tier 1 — Official APIs (durable, zero ban risk)

| API | Endpoint | Returns |
|---|---|---|
| Facebook Graph | `GET /{page-id}?fields=phone,emails,website,single_line_address,link` | Public Page contact block |
| Instagram Graph | `GET /{ig-user-id}?fields=business_discovery.username({user}){biography,website,followers_count}` | Public business-account bio + link |

Requires a Meta app, a connected IG Business account, and App Review for *Page Public Content Access* / `instagram_basic`. Review takes weeks — **start the application on day one of the build**, run Tiers 2–3 meanwhile, and cut over when approved. This is the only path with a stable long-term future.

### 6.3 Tier 2 — Search-engine-mediated discovery (never touches the platform)

Instead of crawling Facebook, ask a search index that has already crawled it. FB Pages are indexed and their `og:description` frequently carries the phone directly in the snippet.

```
site:facebook.com "Lahore" "boutique"   ("0300" OR "0321" OR "0333" OR "wa.me")
site:instagram.com "Karachi" "clothing" "03"
```

Run these through a paid SERP API or Bing Web Search API — a legitimate commercial service, no evasion, no ban surface. Harvest the phone straight from title + snippet + cached meta description. Typical yield: **20–35% of pages surface a number without ever loading facebook.com.**

### 6.4 Tier 3 — Bio-link following (highest yield, one page-load)

The key insight for this market. A Pakistani clothing/accessories seller's IG bio link goes to one of:

- **`wa.me/92XXXXXXXXXX`** — the number, confirmed WhatsApp, done
- **Linktree / beacons.ai / bio.link / taplink / campsite.bio** — plain public HTML, no auth, and virtually always contains a WhatsApp button
- **A Shopify / WooCommerce / Wix store** — hand straight to your §5.2 website module

So the pipeline is:

```
IG profile (ONE logged-out page load)
   └→ og:description  → bio text, often contains "03XX..." inline
   └→ bio link URL
        ├→ wa.me/...        → DONE, confirmed
        ├→ linktr.ee/...    → fetch, extract WhatsApp button  → confirmed
        └→ store.pk         → run §5.2 website module         → confirmed/likely
```

You touch Instagram exactly once per business, logged out, at a rate no different from a human browsing. Everything downstream is unauthenticated public web. **This tier alone should deliver the bulk of your fashion/ecommerce numbers.**

For Facebook Pages the equivalent hop is the Page's `website` field → run the website module.

### 6.5 Tier 4 — Operator-assisted review

For high-value leads the pipeline couldn't resolve, queue them in the UI with a direct link. Your operator, logged into their own normal account, looks and types the number in. Human in the loop, no automation against the platform, no ban risk. Cap this at ~20 per run so it stays a few minutes of work.

### 6.6 Operating rules for this module (all tiers)

- Logged out only. No credential store, no session pool, no account rotation.
- **Hard cap: 1 request per business per run.** Cache the result for 30 days.
- 8–20s randomised delay between profile loads; concurrency 1.
- Respect `robots.txt` and honour `429`/`503` with exponential backoff, then **stop the module for the run** — don't grind against a refusal.
- If a wall appears, record `blocked` on the record and move on. A blocked record is a valid outcome, not a failure to route around.
- Toggle defaults to **off**. Treat FB/IG as a 5–10% uplift on top of a run that already succeeded without it.

---

## 7. Anti-blocking (the legitimate kind)

These measures exist for **reliability and politeness**, not concealment.

| Technique | Purpose |
|---|---|
| Content-addressed cache (URL hash → raw body) | Cuts request volume 60–80%. Biggest single win |
| Concurrency 2–4, never 20 | Stays under rate limits by design |
| Randomised 3–10s delays, jittered | Avoids bursty patterns that trip naive limiters |
| Persistent browser profile per worker | Cookies/consent state persist; fewer interstitials |
| PK residential egress IP | **Required for Maps** — results are geo-ranked; a US IP returns wrong data |
| Exponential backoff on 429/503, then circuit-break | Stop hammering a source that's refusing |
| Save every raw response | Re-parse on selector breakage without re-fetching |
| Per-source daily request budget | Hard ceiling, enforced in code |

Circuit breaker: 3 consecutive failures or any CAPTCHA on a source → pause that source for 30 minutes, log it, surface it in the UI. Continue the run with remaining sources.

> **Qualified by measurement, Aug 2026.** "Source" and "host" are the same thing for Maps, Zameen or BusinessList and are *not* the same thing for §5.2 business websites, which is a few hundred unrelated hosts behind one module name. Applying this rule per source there let one salon's WAF stop the crawler for 19 other salons. Where a module fans out over many independent hosts, a refusal breaks that **host**; only a streak of refusals across consecutive hosts breaks the source. See the §5.2 note for the measured case and the thresholds.

### 7.1 External dependencies — what you must buy vs. what's free

Consolidated procurement checklist. **Only one item is genuinely required.**

| Dependency | Status | Used by | Notes |
|---|---|---|---|
| **PK residential proxy** | **Required** | Google Maps | Maps results are geo-ranked — a non-PK IP returns the wrong businesses entirely. This is a correctness issue, not an evasion one. Budget by GB; Maps panels are light, a full run is well under 1 GB |
| SERP API (Serper, ScraperAPI, Bing Web Search) | Optional | FB/IG Tier 2 (§6.3) | Only needed if you enable the social toggles. Priced per query; a run uses a few hundred |
| Meta Graph API | Optional, free | FB/IG Tier 1 (§6.2) | No cost, but App Review takes weeks. Start the application at Phase 1 |
| Google Maps Places API | **Not used** | — | Deliberate: pricing at this volume is prohibitive and per-request quotas cap the grid fan-out. The browser path is cheaper and less constrained |
| Everything else | Free / self-hosted | Websites, directories, PakPlay, Turfy, Zameen, UrduPoint | Plain fetch or your own Playwright workers. No third-party cost |

So a v1 running core sources only needs **one paid input: a PK residential proxy.** Add the SERP API only when you switch the FB/IG toggles on.

---

## 8. Person attribution (owner / CEO)

The hardest stage. Be honest in the data model about what tier each attribution came from rather than forcing a single "owner number" column you can't fill.

| Tier | Situation | Method | Confidence |
|---|---|---|---|
| **A** | Real estate | Zameen agency page lists named staff with direct mobiles — name and number already paired | 0.85–0.95 |
| **B** | Small food / salon / car / entertainment | The listed number *is* the owner's personal cell. Family businesses have no switchboard | 0.55 (inferred, never claimed) |
| **C** | Registered companies | SECP public register lists directors (names only, no numbers). Most SMBs are sole proprietorships → thin coverage | 0.70 name / 0.0 number |
| **D** | Any | Site "About/Team" page, JSON-LD `founder`, FB Page owner post, LinkedIn "Owner at X" | 0.40–0.70 name / unlinked number |

**Attribution rules:**

- A name and a number are only *linked* when they appear in the same DOM block or the same structured record. Proximity in the same block → `linked`. Same page but distant → `co_occurring`. Never fabricate the join.
- Tier B assigns `person_role = "likely_owner"` and `attribution = "inferred"`. It is never labelled `confirmed`.
- Regex the honorifics that actually appear in PK listings: `Owner|Proprietor|CEO|Director|Founder|Managing Partner|Malik|Sahib`.

---

## 9. Phone normalisation and WhatsApp evidence

### 9.1 Extraction regex (pre-normalisation)

```regex
(?:(?:\+|00)?92[\s\-.]?|0)(3\d{2}|\d{2,3})[\s\-.]?\d{6,8}
```

Formats seen in the wild, all verified in live source data:
`0300-1234567` · `+92 300 1234567` · `03001234567` · `(92 42) 35772057` · `+92-42-35771025` · `042 111 117 638` · `(021) 111 339 339` · `92 52 3258881`

**Reject before normalising:** CNIC numbers (13 digits, `xxxxx-xxxxxxx-x`), NTN, prices, years, plot numbers, and any digit run adjacent to `Rs`/`PKR`.

### 9.2 Normalise and classify

Use `phonenumbers` with region `PK` → E.164 (`+923001234567`).

| Prefix | Operator | Type | WhatsApp likelihood |
|---|---|---|---|
| `030x` | Jazz / Mobilink | mobile | high |
| `031x` | Zong | mobile | high |
| `032x` | Warid (Jazz) | mobile | high |
| `033x` | Ufone | mobile | high |
| `034x` | Telenor | mobile | high |
| `035x` | SCO | mobile | medium |
| `021` Karachi · `042` Lahore · `051` Isb/Rwp · `041` Faisalabad · `061` Multan · `091` Peshawar · `055` Gujranwala · `052` Sialkot · `022` Hyderabad · `081` Quetta | — | landline | **none** |
| `111-xxx-xxx` (UAN) | — | UAN | none |

### 9.3 WhatsApp evidence scoring

There is **no legitimate API to check whether a number has WhatsApp.** Automating WhatsApp Web to test contacts violates WhatsApp's terms and gets the testing number banned within days. Score on evidence instead:

| Evidence | Score | Label |
|---|---|---|
| `wa.me/92...` or `api.whatsapp.com/send?phone=` link found | 1.00 | **confirmed** |
| WhatsApp chat widget with matching `data-phone` | 0.95 | **confirmed** |
| FB Page WhatsApp button / IG "WhatsApp" action | 0.90 | confirmed |
| Text "WhatsApp" within 50 chars of the number | 0.75 | likely |
| `03xx` mobile, no other signal | 0.60 | likely |
| Landline / UAN | 0.00 | no |

Export the label, not the raw score, in the user-facing table.

---

## 10. Deduplication and lead scoring

### 10.1 Dedupe cascade

1. **Exact** — normalised E.164 phone match → same business
2. **Strong** — Google `place_id` match
3. **Fuzzy** — `rapidfuzz` token-set ratio on normalised name ≥ 88 **AND** haversine distance < 150m
4. **Domain** — same registrable domain on website

Merge strategy: keep the highest-confidence value per field, **union all contacts**, union all `source_urls`. Never discard a contact during merge — a second number is a second column, and that's exactly what you asked for.

### 10.2 Lead score (0–100)

```
score =  30 × whatsapp_evidence          (0–1)
       + 25 × contact_confidence         (0–1)
       + 15 × person_attribution         (0–1)
       + 10 × source_agreement           (n_sources ≥ 2 → 1.0)
       + 10 × business_signal            (reviews/rating, normalised)
       + 10 × completeness               (fields populated ratio)
```

Default the table sort to `lead_score DESC`. "Good quality lead" = score ≥ 60 **and** at least one mobile. Report both raw and qualified counts in the run summary.

---

## 11. Data model

```sql
CREATE TABLE runs (
  id              UUID PRIMARY KEY,
  city            TEXT NOT NULL,
  category        TEXT NOT NULL,
  subcategories   TEXT[],
  number_pref     TEXT NOT NULL,
  sources_enabled JSONB NOT NULL,
  status          TEXT NOT NULL,       -- queued|running|done|failed|partial
  stats           JSONB,               -- per-stage counters
  started_at      TIMESTAMPTZ,
  finished_at     TIMESTAMPTZ
);

CREATE TABLE businesses (
  id            UUID PRIMARY KEY,
  run_id        UUID REFERENCES runs(id),
  name          TEXT NOT NULL,
  name_norm     TEXT NOT NULL,
  category      TEXT,
  subcategory   TEXT,
  city          TEXT,
  area          TEXT,
  address       TEXT,
  lat           NUMERIC(9,6),
  lng           NUMERIC(9,6),
  place_id      TEXT UNIQUE,
  website       TEXT,
  facebook_url  TEXT,
  instagram_url TEXT,
  rating        NUMERIC(2,1),
  review_count  INT,
  lead_score    INT,
  created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE contacts (
  id             UUID PRIMARY KEY,
  business_id    UUID REFERENCES businesses(id) ON DELETE CASCADE,
  kind           TEXT NOT NULL,     -- phone | email
  value_raw      TEXT NOT NULL,
  value_e164     TEXT,
  line_type      TEXT,              -- mobile | landline | uan
  operator       TEXT,
  wa_evidence    NUMERIC(3,2),
  wa_label       TEXT,              -- confirmed | likely | no
  person_name    TEXT,
  person_role    TEXT,              -- owner | ceo | director | agent | likely_owner
  attribution    TEXT,              -- confirmed | linked | co_occurring | inferred
  confidence     NUMERIC(3,2),
  source         TEXT NOT NULL,     -- google_maps | website | pakplay | zameen | ...
  source_url     TEXT NOT NULL,
  rank           INT,               -- 1..N after preference ranking
  scraped_at     TIMESTAMPTZ
);

CREATE TABLE raw_fetches (
  url_hash    TEXT PRIMARY KEY,
  url         TEXT,
  status      INT,
  body_path   TEXT,
  fetched_at  TIMESTAMPTZ
);

CREATE INDEX ON contacts (business_id, rank);
CREATE INDEX ON contacts (value_e164);
CREATE INDEX ON businesses (run_id, lead_score DESC);
```

---

## 12. Output table and CSV export

### 12.1 Column set

Phone columns are **generated dynamically**, ordered by the `number_preference` ranking, capped at 4.

| # | Column | Notes |
|---|---|---|
| 1 | `business_name` | |
| 2 | `category` | |
| 3 | `subcategory` | |
| 4 | `city` | |
| 5 | `area` | Neighbourhood / commercial tile |
| 6 | `address` | |
| 7 | `contact_person` | Blank when unattributed — never guessed |
| 8 | `contact_role` | owner / ceo / director / agent / likely_owner |
| 9 | `attribution` | confirmed / linked / co_occurring / inferred |
| 10 | `phone_count` | Integer |
| 11 | `phone_1` | E.164 |
| 12 | `phone_1_type` | mobile / landline / uan |
| 13 | `phone_1_whatsapp` | confirmed / likely / no |
| 14 | `phone_1_belongs_to` | owner / business / agent |
| 15 | `phone_1_source` | |
| 16–20 | `phone_2_*` | Same 5 columns (value + 4 attributes) |
| 21–25 | `phone_3_*` | |
| 26–30 | `phone_4_*` | |
| 31 | `email` | |
| 32 | `website` | |
| 33 | `facebook_url` | |
| 34 | `instagram_url` | |
| 35 | `maps_url` | |
| 36 | `rating` | |
| 37 | `review_count` | |
| 38 | `lead_score` | 0–100 |
| 39 | `sources` | Pipe-delimited |
| 40 | `evidence_urls` | Pipe-delimited |
| 41 | `scraped_at` | ISO 8601 |

**41 columns total, but only ~12 concepts.** The count is inflated by the phone block: 4 slots × 5 columns = 20 of the 41. Grouped:

| Group | Cols | What it is |
|---|---|---|
| Identity | 6 | Who and where the business is |
| Person | 3 | Name, role, how confident the attribution is |
| Phones | 21 | `phone_count` + 4 ranked slots |
| Other contact | 5 | Email, website, 3 profile URLs |
| Quality | 3 | Rating, review count, lead score |
| Provenance | 3 | Sources, evidence URLs, timestamp |

### 12.3 Compact vs full view

Nobody wants 41 columns on screen. Default the table to a **12-column compact view**; everything else lives behind the column-visibility toggle. CSV always exports the full set.

```
business_name · area · contact_person · contact_role
phone_1 · phone_1_whatsapp · phone_2 · phone_count
website · rating · lead_score · sources
```

### 12.2 CSV export

Server-side endpoint `GET /api/runs/{id}/export.csv?filter=...` — generate server-side because the column set is dynamic and the filter state must match what the operator sees on screen.

- UTF-8 **with BOM** (so Excel opens Urdu business names correctly)
- Phones written as `="+923001234567"` to stop Excel mangling them into scientific notation — this bites everyone once
- Filename: `{city}_{category}_{YYYYMMDD}_{n}leads.csv`
- Respect the active table filters and sort order

---

## 13. Frontend specification

Minimal, three screens.

### Screen 1 — New Run

```
┌────────────────────────────────────────────────┐
│  City         [ Lahore          ▾ ]            │
│  Category     [ Salon Services  ▾ ]            │
│  Subcategory  [ ☑ barber ☑ spa ☐ bridal ]      │
│                                                │
│  Number preference                             │
│    ◉ Owner / CEO first                         │
│    ○ Business number first                     │
│    ○ WhatsApp-verified only                    │
│                                                │
│  Sources                                       │
│    Google Maps        [ ON  ] (core)           │
│    Business websites  [ ON  ] (core)           │
│    Directories        [ ON  ] (core)           │
│    Facebook Pages     [ off ]                  │
│    Instagram          [ off ]                  │
│                                                │
│  Target leads   [ 500 ]                        │
│  Est. runtime: ~55 min · Est. available: ~780  │
│                                                │
│              [  Start Run  ]                   │
└────────────────────────────────────────────────┘
```

**Estimated available** is important. For narrow category/city pairs (padel in Faisalabad) the honest number is 30–50, not several hundred. Show it before the run starts so nobody waits an hour for a thin result.

### Screen 2 — Run Progress

Per-stage counters (discovered / enriched / attributed / deduped / qualified), a live log tail, per-source status pills (`ok` / `throttled` / `blocked`), and a Cancel button. Results stream into the table as they qualify — don't make the operator wait for the full run.

### Screen 3 — Results Table

- Sticky header, virtualised rows (TanStack Table + `@tanstack/react-virtual`)
- Sort any column; default `lead_score DESC`
- Filters: WhatsApp status, has-owner-name, min score, phone type, source, free-text
- Column visibility toggle, persisted to `localStorage`
- Green badge on `confirmed` WhatsApp cells, grey on `likely`
- Row expand → evidence panel with source URLs
- **Export CSV** button, top right, exports the current filtered view
- Bulk select → delete (for honouring removal requests)

### Settings

Proxy endpoint + credentials · per-source rate limits and daily budgets · SERP/Meta API keys · cache TTL · dedupe fuzzy threshold · concurrency · default city list.

---

## 14. Throughput and hitting the target

Worked example — **Lahore × salon**, core sources only:

| Stage | Volume | Time |
|---|---|---|
| Maps fan-out (12 tiles × 5 synonyms) | 60 queries | 12 min |
| Raw results | ~2,400 | |
| After cross-query dedupe | ~700 unique | 3 min |
| Maps payload parse (phones included — §5.1) | 60 responses | ~0 min |
| Maps detail panels (fallback only, ~5% of records) | ~35 | 2 min |
| Website enrichment (~30% have sites) | 210 domains | 8 min |
| Directory corroboration | ~120 matches | 4 min |
| Normalise, attribute, score, dedupe | — | 2 min |
| **With a phone** | **~600** | |
| **Qualified (score ≥ 60 + mobile)** | **~380** | **~31 min** |

> **Revised Aug 2026.** This table previously budgeted 700 detail-panel interactions at 28 minutes, giving ~57 min per run. Phones are already in the search payload (§5.1), so panels are now a fallback for the ~13% of records without one rather than the main path. The old 28-minute figure also silently assumed ~4 parallel browser workers to reconcile with §5.1's stated 200–500 businesses/hour *per browser*; on a single worker it would have been closer to two hours.

> **Website row confirmed, Aug 2026 (Phase 3).** The "~30% have sites" assumption in that row is the one figure in this table that has now been measured end to end, and it holds: **32%** of the 199 discovered Islamabad businesses carry a real website (39% carry any URL, the rest being FB/IG profiles that route to §6). Note this *contradicts* the ~85% website fill §5.1 used to claim — see the correction there; the 85% was a first-page sample. Crawl depth came in at **1.5 pages per domain** against the 4-page budget, so the 8-minute estimate has headroom rather than risk.

**Measured yield, and what it means for the plan.** 3 queries (Islamabad × salon, 1 synonym, 3 tiles) produced **199 unique businesses, 174 with a phone**. At that rate the §5.1 full fan-out of 60 queries overshoots the 300–800 target by a wide margin — the practical constraint becomes runtime and politeness, not availability. Two consequences worth acting on:

- **Tune the plan down, not up.** For a broad category in a tier-1 city, 3–4 synonyms × 6–8 tiles is likely enough to clear the target. Spending 60 queries to collect 3,000 businesses you will not contact is a worse trade than spending 20 and finishing in a third of the time.
- **The §4.2 synonym list has heavy internal overlap.** Measured on Lahore × salon, 8 queries over 4 near-synonyms (`salon`, `saloon`, `beauty parlour`, `beauty parlor`) returned 160 raw for 52 unique — a 67% duplicate rate. Near-duplicate spellings earn their place only in thin markets. Rank synonyms by *marginal* unique yield after the §16 validation run and cut the tail.

Comfortably inside the "few hundred good-quality leads" target for broad categories in tier-1 cities.

**Where it won't hit:** narrow categories in smaller cities. Entertainment in Faisalabad might total 40 venues in existence. Surface the honest estimate up front rather than padding the table with junk.

**Scaling levers, in order of value:** more synonyms → more tiles → adjacent-city rollup → enable FB/IG (+5–10%).

---

## 15. Compliance

- **Business contact data only.** Numbers a business publishes for customer contact. Do not collect personal numbers of individuals who aren't holding the business out for contact.
- **Platform terms.** Facebook, Instagram, LinkedIn and TikTok prohibit scraping in their ToS. The module in §6 is built around official APIs and unauthenticated public surfaces for that reason. Maps, business websites and directories carry materially less risk for equal or better yield.
- **WhatsApp outreach reality.** The Business API requires opt-in for template messages. Cold-blasting from a personal or Business-app number gets it banned within days. Practical sequencing: first touch by call or email, WhatsApp for warm follow-up. Also note PTA rules on unsolicited commercial messaging and PECA 2016.
- **Provenance and deletion.** Every contact row carries `source_url` and `scraped_at`. Build the bulk-delete path in v1 — you will need it, and retrofitting it is painful.
- **Suppression list.** A `do_not_contact` table checked at export time. Anyone who asks to be removed goes in permanently, and it survives re-runs.

---

## 16. Build order

| Phase | Deliverable | Est. |
|---|---|---|
| **1** | Schema, queue skeleton, raw-fetch cache, phone normaliser + PK classifier | 3–4 d |
| **2** | Google Maps module (grid fan-out, network interception, detail panels) | 5–7 d |
| **3** | Website module (wa.me, widgets, tel:, JSON-LD) | 2–3 d |
| **4** | Dedupe + scoring + ranking by `number_preference` | 2–3 d |
| **5** | Frontend: run form, progress, table, CSV export | 4–5 d |
| **6** | Directory modules (BusinessList, UrduPoint) | 2–3 d |
| **7** | Vertical modules (PakPlay, Turfy, Zameen click-reveal, Foodpanda) | 4–5 d |
| **8** | FB/IG Tiers 2–3 (SERP + bio-link follow) | 3–4 d |
| **9** | Person attribution engine | 3–4 d |
| **10** | Meta API cutover (start review at Phase 1) | 1–2 d + review wait |

**Ship after Phase 5.** That's Maps + websites + a working table with export — roughly 80% of the value, and it tells you whether the real hit rate matches §14 before you invest in the long tail.

### Validation before scaling

Run **Lahore × salon** and **Lahore × food**, hand-check 50 random rows:

- Is the phone correct and reachable?
- Is `phone_1` genuinely the best number under the chosen preference?
- Do `confirmed` WhatsApp labels hold up when dialled?
- What's the true duplicate rate after merge?

Tune the scoring weights against that ground truth before building more source modules. Everything downstream depends on those weights being right.