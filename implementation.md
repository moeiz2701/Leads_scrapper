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

**Note — decided and built, Aug 2026 (Phase 5). RQ, not synchronous.** Phase 1 left the queues with no producer and no consumer; stages ran synchronously from `scripts/`. That worked, and a full Islamabad run is only ~5 minutes, so the choice was genuinely open. It went to the queue on this section's own argument — *"you need to re-run stage 3 without re-running stage 1"* — because that makes a stage the unit of retry and re-running one a **normal operation, not a recovery**. `POST /api/runs/{id}/stages/{stage}` is that operation, and it does not chain. Three consequences followed rather than motivated it: a 5-minute blocking POST is a timeout on any proxy between the browser and the API; §13 Screen 2's counters must move *while* work is in flight, which a blocking request cannot do; and Cancel needs something to cancel.

Two implementation notes worth recording:

- **Stages chain themselves; RQ's `depends_on` is unused.** Each job enqueues its successor on success, which keeps the failure semantics in code that knows what §5.5 means. It also means there is at most **one job in flight per run**, which is what makes Cancel's behaviour describable in one sentence (below).
- **`QUEUE_SYNC=true` collapses the whole thing to the synchronous mode**, because self-chaining plus RQ's `is_async=False` runs the entire pipeline inside the caller. The API code path is identical in both modes, so they cannot drift. The test suite uses it.

**RQ's default worker cannot run on Windows** — it forks per job and `os.fork` does not exist there. `scripts/worker.py` selects `SimpleWorker` when `os.fork` is absent. The cost is real: a job that takes the process down takes the worker with it, where a forking worker would have survived and marked the job failed. `jobs.run_stage_job` records the failure on the run *before* re-raising, so the record survives even when the process does not.

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

**Note — Phase 4.** Implemented in `core/ranking.py`, written onto `contacts.rank`. Three things the table above leaves open:

- **"Main business line" cannot mean "not a mobile"**, because the same row of the table puts plain `landline` in `business_first`'s *bottom* tier. It is read as the number the business publishes as its own: a UAN, which exists for no other purpose, or the number on its own Maps listing, which the business itself registered. Landlines found elsewhere fall through to the residual tier.
- **The `whatsapp_only` filter is expressed as `rank = NULL`, never as a delete.** §10.1 says never discard a contact and §15 needs the row for provenance, so an excluded number keeps its row and only loses its export slot — switching the preference back is lossless. The filter keeps the `likely` band: §9.3 scores a bare `03xx` at 0.60 precisely because a PK mobile probably does take WhatsApp, and restricting it to `confirmed` would cut the Islamabad run from 256 numbers to 53 and read to the operator as a broken run rather than as a filter they chose.
- **A number takes at most one ranked slot.** After a §10.1 merge the same E.164 can sit on two rows of one business, each a real provenance record. The better-evidenced row wins the slot and its duplicate goes unranked, so §12.1 never shows the operator the same number as both `phone_1` and `phone_2`.

**Note — decided, Aug 2026 (Phase 5). Re-ranking stays in Stage 5.** When the operator changes `number_preference` the alternative was to rank at read time in the exporter, which would avoid a re-run entirely. Both are defensible; ranking stayed in the stage for one reason: **`contacts.rank` is a real, indexed column that §12.1, §13 Screen 3 and the CSV all read**, so it must never disagree with the run's preference, and one writer is how that stays true. Read-time ranking would also fork §3.3 into two implementations — a sort in the exporter and a column in the database — that could rot apart silently. `PATCH /api/runs/{id}/preference` therefore updates `runs.number_pref` and enqueues Stage 5 alone. It is pure database work and measured at **0.2 s** on the 60-business Lahore run, so correctness wins outright rather than on a trade.

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
- **Yield varies enormously by city/category slice** — 45% of crawled Islamabad domains produced a confirmed number against 13% in Lahore, on the same code and the same category. The Islamabad salon set skews to clinics and spas with real websites. **Measure per slice; do not extrapolate one run's confirmation rate into the §13 "estimated available" figure.** *(Third slice, Aug 2026: **Lahore × food, 7%** — 13 businesses with a confirmed number from 182 domains crawled. Two Lahore slices now bracket each other at 13% and 7% while Islamabad sits at 45%, so the Islamabad skew this bullet suspected is the better explanation than anything about Lahore. The rule stands and is now measured on three slices rather than two.)*

- **Website fill is a category property, not a constant.** §14's "~30% have sites" was measured on salons and held at 32%. Food comes in at **49%** (210 of 429), and Lahore salons at 60% of a much smaller base. The website pass is therefore a bigger lever in food than the §14 table assumes — 182 domains crawled off 6 queries, against 62 off 3 for Islamabad salons.

- **Restaurants publish a different kind of number, and it costs them qualification.** The food run's phone mix is **69% mobile / 19% landline / 12% UAN** against the salon run's 80/19/1. A twelvefold difference in UAN share is a real property of the vertical — restaurants publish reservation lines — and UANs carry no WhatsApp evidence, so food qualifies at **15%** against salon's 23–37% despite returning seven times the businesses. Volume and qualification move independently; §14's throughput table conflates them.
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

**Note — built and measured, Aug 2026 (Phase 6). The coverage warning above is exactly right and still understates the problem. This layer was built, it works, and it yields nothing.** Modules: `sources/businesslist.py`, `services/directories.py`, `scripts/spike_directories.py`, `scripts/run_directories.py`.

**Three of the four sources in the table above are refused**, on measurement rather than on the table's prose. Each is recorded in `taxonomy.EXCLUDED_SOURCES` with its reason, the way §4.1's exclusions are, so nobody re-adds one without reading why:

| Source | What recon found |
|---|---|
| **UrduPoint** | The table's "clean field table (`Mob #`), mostly `03xx`" with an owner-name field does not describe what it serves. Sampled over the **21** Lahore restaurant records its directory actually holds: **4% carry a mobile, 85% a landline, 0% an owner name.** §9.3 scores a landline 0.00 and §10.2 requires a mobile to qualify, so it cannot produce a qualified lead. It also costs **one request and 171 KB per business** — its listing pages carry no phone at all — against BusinessList's ~2.5 KB. Its taxonomy is industrial ("wool", "neon sign mfrs", "packing and crating") and contains **no salon or beauty category in any city** |
| **BusinessBook.pk** | Never reaches the point of needing the browser §5.3 warns about: `/category/{slug}-{id}` answers **500**, and the root alternates between a 73 KB body and an empty one across consecutive requests. An unstable source does not earn the Playwright dependency |
| **YellowPage.pk** | The table's own verdict — "low quality, high duplication" — plus a 670 KB category index whose listings are not in the served HTML |
| **listing.com.pk** | Serves a Cloudflare interstitial on every path including the root. That is an access control, and §5.5 draws the line there: automate interactions with public data, never automate past one |

**BusinessList.pk is richer than this section says, and that is what made it worth trying.** The section says "phones in plain text" and does not say *where*. They are on the **listing** page, alongside the address, rating, review count, a stable `data-cmpid`, and — the reason to build it at all — **latitude and longitude** in `div.mapmarker[data-ltd][data-lng]`. So a category page is ~20 complete businesses for one request, no detail fetch is needed for any field §12.1 exports, and §10.1's tier 3 has coordinates on both sides for the first time. Measured over 58 listings: **84% carry a phone, 65% coordinates**, line mix **55% mobile / 40% landline / 4% UAN**. Pagination is a `<link rel="next">`; `robots.txt` disallows only `/admin/`, `/edit/*`, `/sign-in/*` and the search helpers, and permits `/category/*` and `/company/*` outright.

**Where directories enter, decided: Stage 2, after the website pass.** §2 lists them under both discovery and contact enrichment and does not choose. The coverage numbers choose for it — 59 restaurants and 18 beauty salons in the whole of Lahore against Maps' 429 and 60 for the same slices — so as discovery this is a rounding error, while as a second opinion on businesses Maps already found it would (in principle) feed §10.2's `source_agreement`, the one term that no run without websites can currently earn. It runs *after* §5.2 so that the website's stronger evidence is already recorded when the directory's bare number arrives and the upgrade-only rule has something to refuse to overwrite. Stage 2 now has two independent inputs and reports them separately in `runs.stats`; `planned_stages` schedules the stage if *either* flag is set.

**The category taxonomy is self-assigned by the businesses that submit the listing, so a slug's name is not evidence of its contents.** This cost a full measurement cycle before it was noticed. `food-retailers` sounds like restaurants and holds **854** listings of groceries, mineral-water plants and B2B wholesale — Erie Mineral Water, Faysal Karyana Store, Skylark Engineering. `textile` (1,842) is fabric mills, not §4.2's fashion retail. The 18-listing `beauty-salons` figure this section quotes contains **`Bigbasket.pk`, a grocery site** — one business appears in `beauty-salons`, `catering` and `food-retailers` at once. Five further plausible slugs (`food-drink`, `health-beauty`, `property`, `computers-internet`, `entertainment-media`) are parent *index* pages carrying no businesses at all. Every slug in `CATEGORY_SLUGS` was opened and read before it was added; `scripts/spike_directories.py --categories` re-runs the audit.

**The result, over four live slices. This is the finding.**

| Slice | Maps businesses | Directory listings | Matched | **Contacts added** |
|---|---|---|---|---|
| Lahore × food | 428 | 97 | 3 | **0** |
| Islamabad × salon | 199 | 35 | 2 | **0** |
| Lahore × salon | 60 | 109 | 1 | **0** |
| Karachi × salon | 39 | 92 | 1 | **0** |
| **Total** | **726** | **333** | **7 (2.1%)** | **0** |

Zero contacts added, zero businesses corroborated, zero fields gap-filled. Two structural reasons, and neither is a tuning problem:

- **The tiers that fire all require a shared phone, so a match is by construction a number we already had.** `corroborated_geo` and `phone_unlocated` both need an exact phone match — the first to lower the name bar, the second because it is the only evidence available without coordinates. The one tier that can match a business whose numbers we *don't* share is `fuzzy_name_geo`, and it produced **0 matches in 333 listings**.
- **BusinessList publishes geocoded approximations, not surveyed positions.** That is why tier 3 never fires. Matching on name alone and then measuring the distance: "Dilara's Salon" ↔ "Dilara Salon" are **391 m** apart, "HairSense" ↔ "HairSense (Men's Hair Salon Islamabad)" **247 m**, one Lahore pair **13,196 km**. §10.1's 150 m radius was calibrated Maps-against-Maps, where both sides come from one surveyed source; across sources with different coordinate provenance it is too tight.

**Widening the radius was tested and rejected on the numbers.** At 500 m the two richest slices admit five name-matched pairs. Four add nothing — the directory's number is one Maps already has, or the row has no number. The fifth adds a number, and it is a **false match**: "Hair and Hair" ↔ "Ashley's Hair and Makeup Studio", 329 m apart, which token-set ratio scores ≥ 88 because the short name is a subset of the long one. So widening buys **zero correct contacts and one incorrect one**. 150 m stays, and it stays for a measured reason rather than an inherited one.

**Treating unmatched rows as discovery was tested and rejected too.** §5.3's other reading — a thin discovery source where Maps is weak — got its best case: Karachi × salon, only 39 Maps businesses and 0 qualified. Inserting all 91 unmatched listings produced businesses with a **mean score of 26.7 against the Maps rows' 42.3, a maximum of 45, and not one qualified lead**; 20 of the 91 had no phone at all. This is §10.2's discovery-only finding restated — a bare number is not a qualified lead, whatever publishes it. The behaviour exists behind `DIRECTORY_INSERT_UNMATCHED`, off by default, and the run was restored to its 39 rows afterwards.

**Disposition: built, tested, and defaulted off.** §3 lists directories as "core, default on"; that is now `False` in `SourceFlags`, because a source that is on by default and always contributes zero is §5.5's failure mode wearing a toggle. The run-create endpoint states the measurement rather than an adjective. The module is kept rather than deleted: it is correct, it is cheap (6–9 requests), the finding is reproducible, and the next contributor who reads "directories are a corroboration layer" in this section needs the numbers that say what that is worth.

**What this predicts about Phase 7, and it is the useful part.** The failure is entirely a *join* failure — the directory holds real Lahore restaurants (Cafe Zouk, Butt Karahi, Texas Chicken) that this Maps run never surfaced, and we cannot safely attach any of them. §5.4's PakPlay does not have this problem **by construction**: it embeds the Google Maps `place_id` in its venue-page map iframe, which is §10.1's tier 2 — an identity assertion that merges on its own, with no name ratio and no distance test involved. Phase 6's result is therefore an argument *for* Phase 7 rather than against more sources: the constraint is joinability, and PakPlay is the one remaining source that ships its own join key.

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

### 6.7 Note — §6 measured for the first time, Aug 2026

Until this note, §6 was the only section in this document carrying no correction
at all: every number above was written before anything was fetched. §5.3 read the
same way and Phase 6 measured it at zero, so §6 was reconned before a line of the
module was written. `scripts/spike_social.py` reproduces all of it, and every
body is in the §7 archive, so re-checking any figure below costs no requests.

Method: 20 Instagram and 20 Facebook URLs taken from the seven live runs, fetched
logged-out; then 20 IG and 12 FB of the same set **rendered** in a logged-out
browser; then 20 targeted Serper queries against businesses whose Instagram
handle we already hold, so the join could be checked against ground truth rather
than against a similarity score.

**The headline: §6.4's mechanism is wrong and §6.4's substance is right.**

| §6 claim | Plain fetch (§6.4's "no browser") | Rendered, logged out |
|---|---|---|
| IG returns `og:description` | **0 / 20** | 20 / 20 |
| IG bio text available | **0 / 20** | **20 / 20** |
| IG bio link present | **0 / 20** | 14 / 20 |
| FB Page reachable at all | **0 / 20 — HTTP 400** | **12 / 12 — HTTP 200** |

A logged-out `httpx` GET of an Instagram profile returns HTTP 200 and ~605 KB of
JS application shell whose `<title>` is the word `Instagram`. There is no bio, no
`external_url`, no `bio_links`, no phone. A logged-out GET of a Facebook Page
returns **HTTP 400** and a 1,542-byte "Sorry, something went wrong" error page —
not a login wall, not a challenge — for every URL variant tried (`www`, bare,
`/about`, `m.facebook.com`).

**So §6.4's "ONE logged-out page load … no browser" is false as written, and its
"the phone number is one hop away, on a page with no wall at all" is true.** The
page needs rendering, not credentials. Rendering a public page the way a browser
renders it stays inside §6.1: no login, no credential store, no cookie injection,
no fingerprint work — vanilla Playwright with a default context. What changes is
the *cost*, and §14 should read it as such: Tier 3 is a browser tier priced like
Maps discovery, not a cheap `httpx` tier priced like §5.2.

**Correction to §6.4's parsing instruction.** The bio is **not** in
`og:description`. On Instagram `og:description` is `"147K Followers, 179
Following, 5,777 Posts"` and can never contain a phone number; the bio text lives
in `<meta name="description">`. Reading the tag §6.4 names measures this tier at
0/20 when it is really 10/20. This cost one full measurement pass to find.

**The two tiers invert. Facebook is the confirmation engine, not Instagram.**

| Rendered, logged out | Instagram (n=20) | Facebook (n=12) |
|---|---|---|
| Profile/Page content rendered | 20 / 20 | 12 / 12 |
| Bio/description text served | 20 / 20 | 12 / 12 |
| Inline `03xx` mobile in the bio | **10 / 20 (50%)** | 1 / 12 (8%) |
| Bio link present | 14 / 20 | 11 / 12 |
| **WhatsApp link/button in the page** | **2 / 20 (10%)** | **7 / 12 (58%)** |

§6 orders Instagram first and calls Tier 3 "highest yield". For the label that
actually matters it is backwards. **58% of Facebook Pages carry an
`api.whatsapp.com/send?phone=…` button** — §9.3's 0.90 row, `confirmed` — against
10% of Instagram profiles. Instagram's strength is different and still real: half
of its bios print a mobile in plain text, and one bio carried five (Rina's
Kitchenette lists a number per branch). Those are §9.3 0.60 *likely* unless
something else lifts them, which is the same score 850 of our 898 businesses
already carry — so Instagram's inline numbers are worth having for the 47
businesses with no phone at all, and are close to a constant everywhere else.

Instagram's bio-link destinations (n=20): **store 11, none 6, social 2, wa.me 1.**
§6.4 presents its three-way branch as though the WhatsApp branch were the common
one. It is the rarest. The common branch is a store URL, which is a hand-off to
the §5.2 module that already exists.

**A free feeder nobody costed: Facebook → Instagram.** 6 of 12 Facebook bio links
are Instagram profiles. That is a platform-to-platform join needing no SERP
credits, no name matching and no join test — the Page says which account is its
own.

**§6.3 measured, and it is a feeder, not a number source.**

| §6.3 claim | Measured |
|---|---|
| "20–35% of pages surface a number" | **3 / 20 (15%)** of snippets carried a mobile |
| Targeted `site:instagram.com "<name>" <city>` finds the right profile | **6 / 20 (30%)** |
| Recoverable further down the results | **0** — when the top hit is wrong, the known handle is nowhere in the top 10 either |

And a trap: **11 / 20 top hits score ≥88 name similarity but only 6 are the right
profile.** The high-scoring wrong ones are `instagram.com/popular/<slug>` location
pages and `/p/` and `/reel/` permalinks, which carry the business name and are not
profiles. A name ratio is therefore *not* a usable join test here. Filtering to
bare-handle profile URLs gives **6 correct out of 7 accepted, at 30% recall** —
high precision, low recall, which is the right shape for a join and the wrong
shape for a volume source. §6.3's own literal example is a *broad* query; anything
it finds has no `place_id`, no coordinates and no address, which is exactly the
unjoinable population that made §5.3 yield nothing. Use targeted queries only.

**What the name ratio cannot catch, and what to be honest about.** Entity match
on the URLs we already hold is good — 30 of 32 rendered pages score ≥61 name
similarity against the business we asked about. The residual risk is not
mis-naming, it is **branch versus brand**: "Tao Pan - Lahore" resolves to the
Gujranwala Page, "The Carnivore Lahore" to the Islamabad one, "Butlers Chocolate
Café" to Butlers Chocolates of Dublin. All three score 85–100 on name, because
the name *is* right. A number harvested from a national brand Page is a real,
contactable number that does not belong to the branch in the row. Name similarity
cannot see this; only `source_url` and `wa_evidence_url` can, which is why §1's
provenance rule is what makes this tier auditable rather than merely plausible.

**Two §6.6 rules survive the measurement unchanged and one gains a reason.** The
30-day cache and the 1-request-per-business cap are right. The 8–20s delay is now
also a browser-launch budget. And "a `blocked` record is a valid outcome" is the
rule that stops the FB HTTP 400 from being read as something to route around: it
is what a non-browser client gets, and the answer is to render or to record
`blocked`, never to dress the client up as something it is not.

**A §6.1 boundary that was checked and not crossed.** Meta serves `og:` tags to
link-preview crawlers, so claiming to be `facebookexternalhit` is a way to get
metadata a browser is refused. `spike_social.py --ua-probe` exists to measure that
difference and report it; nothing in the module uses it. Presenting a false
identity to obtain content otherwise withheld is the thing §6.1 rules out, and
the rendered path makes the question moot — the browser gets the page honestly.

### 6.8 Note — Tier 3 run live on three slices, Aug 2026

Recon said the tier should work. This is what it did, and it is the first source
module since §5.2 to add anything at all.

| | Lahore × salon | Lahore × food | Islamabad × salon |
|---|---|---|---|
| Businesses | 60 | 428 | 199 |
| With a social URL | 26 | 140 | 55 |
| **Businesses with a `confirmed` number** | 4 → **9** | 13 → **20** | 28 → **30** |
| **Qualified (§10.2 ≥ 60 + a mobile)** | 22 → **26** | 65 → **76** | 45 → **47** |
| Websites gap-filled for §5.2 | +3 | +28 | +5 |
| Phone contacts added | +5 | +22 | +3 |

**+17 qualified leads across the three slices, from 36 new contacts.** The
leverage is §10.2's: a `confirmed` label is worth +12 points against a bare
mobile's 0.60, which is exactly the width of the 50–59 band the score
distribution piles into. That is also the warning — the ≥60 bar has still never
been calibrated and it sits on a spike, so "+17 qualified" is a statement about
the current weights as much as about the data. §16's validation pass is what turns
it into a statement about leads.

Islamabad is the informative one. It is the slice §5.2 already did *best* on — 28
of 199 businesses confirmed, against Lahore × food's 13 of 428 — and Tier 3 still
added 2 more plus 5 websites. The tiers are not substitutes: a business publishes
a WhatsApp button on its Facebook Page or it does not, and that is independent of
whether it also runs a website.

**Re-running the stage is idempotent, and that is worth stating because it was
verified rather than assumed.** A second pass over Lahore × food reported 0
contacts added and 0 upgraded. The merge is upgrade-only on evidence and gap-fill
only on everything else, so the stage can be re-run after a parser fix without
disturbing what earlier passes established.

**The tier's real ceiling is not the 58% button rate — it is the shell rate.**

| | Rendered | HTTP 200, no profile |
|---|---|---|
| Lahore × food | 96 | **44 (31%)** |
| Islamabad × salon | 21 | **34 (62%)** |

These are not junk URLs; they are real Pages and real handles. The page comes back
with the application shell and no `og:` tags. Two things are known about it: it is
**transient** — re-requesting one of them rendered it fine — and it got **worse
across a long session**, 31% early and 62% after roughly 200 cumulative renders.
That is the signature of soft rate-limiting, and it is the honest cap on this
tier: budget on rendering roughly two thirds of what you ask for in one pass, and
re-run to pick up the rest. §6 does not mention this at all.

Two consequences the module encodes:

* **A shell is never cached.** §6.6's 30-day TTL on a non-result would convert one
  transient gate into a month of permanent misses — the re-run would "hit cache",
  find nothing, and report the business as having no bio until September. This is
  the one place §7's "save every raw response" is deliberately not applied, for
  §7's own stated reason: bodies are kept so a broken selector can be re-parsed,
  and there is nothing in a shell to re-parse. The cost is that a re-run is not
  free — it retries them, ~40 requests on Lahore × food.
* **An empty page must not trip the §7 breaker.** `CircuitBreaker` implements
  §5.5's "the selectors stopped matching" rule as five consecutive unproductive
  successes. Counting a shell as unproductive tripped the Facebook breaker 77
  profiles into Lahore × food and blocked the remaining 29 Pages — while **all 77
  renders had returned HTTP 200 and Facebook had refused nothing.** §5.5's check
  belongs at the stage ("rendered 15+ profiles, found zero numbers"), not at the
  request. `sources/businesslist.py` moved the same check up a level for the same
  reason, and §5.2 split host from source for the third version of this mistake.

**Throughput, for §14 and for §13 Screen 1.** 19 s per business, median across 127
live renders (mean 17.9, p90 31.5) — §6.6's 8–20 s delay plus a browser launch and
page load, at concurrency 1. 28% of businesses carry a social URL (248 of 898;
19–33% per slice). On Lahore × food that is ~45 minutes of social against ~12 of
discovery, so **this term dominates any run with the toggles on** and
`services/estimates.py` now includes it. Screen 1 quoting a discovery-only runtime
while the operator has Facebook ticked is exactly the dishonesty §13 forbids.

**Two markup traps, both of which fail silently rather than loudly.** Meta hides
these URLs inside JSON string literals: `\/` for a slash, and Facebook
*double-encodes* its outbound link shim as `u=https%3A%2F…`. A URL
matcher stops at the backslash, so every Facebook bio link truncated to the five
characters `https` — which is indistinguishable in the run stats from "this Page
has no bio link". The run honestly reported 0 websites filled and nothing looked
broken. Separately, `/profilecard/` is Instagram's share-card view and carries no
bio; the same handle without the suffix renders fully. Instagram's tracking
params (`igshid`, `igsh`, `utm_*`) were tested alongside it and make **no**
difference, recorded here so nobody strips them on suspicion later.

**What still cannot be checked automatically.** Entity match is good — 30 of 32
rendered pages score ≥61 name similarity against the business we asked about — but
the residual risk is branch versus brand, and a name ratio cannot see it. "Tao Pan
- Lahore" resolves to the Gujranwala Page, "The Carnivore Lahore" to Islamabad,
"Butlers Chocolate Café" to Butlers of Dublin, all scoring 85–100 because the name
*is* right. Verified on the live data: a number proven this way keeps
`source`/`source_url` pointing at Maps and records the Page in `wa_evidence_url`,
so §1's provenance rule is what makes this auditable. It is the first thing the
§16 validation pass should hand-check.

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
| **PK residential proxy** | **Required** — but see the measurement below | Google Maps | Maps results are geo-ranked — a non-PK IP returns the wrong businesses entirely. This is a correctness issue, not an evasion one. Budget by GB; Maps panels are light, a full run is well under 1 GB |
| SERP API (Serper, ScraperAPI, Bing Web Search) | Optional | FB/IG Tier 2 (§6.3) | Only needed if you enable the social toggles. Priced per query; a run uses a few hundred |
| Meta Graph API | Optional, free | FB/IG Tier 1 (§6.2) | No cost, but App Review takes weeks. Start the application at Phase 1 |
| Google Maps Places API | **Not used** | — | Deliberate: pricing at this volume is prohibitive and per-request quotas cap the grid fan-out. The browser path is cheaper and less constrained |
| Everything else | Free / self-hosted | Websites, directories, PakPlay, Turfy, Zameen, UrduPoint | Plain fetch or your own Playwright workers. No third-party cost |

So a v1 running core sources only needs **one paid input: a PK residential proxy.** Add the SERP API only when you switch the FB/IG toggles on.

**Note — measured on a direct connection, Aug 2026. "Returns the wrong businesses entirely" is stronger than the evidence supports, and the distinction changes what the proxy is for.**

Six live Maps queries for Lahore × food, run with `PROXY_REQUIRED_SOURCES=""` from a non-PK IP, produced 429 unique businesses. Checked against geography rather than assumed:

| Check | Result |
|---|---|
| Address names Lahore | **429 / 429** |
| lat/lng inside the Lahore bounding box | **429 / 429** |
| Phone numbers `+92` | **406 / 406** |
| Landlines on Lahore's `042` area code | **63 / 63** |
| Queries blocked or failed | **0 / 6** |

The top rows are Nishat Hotel, English Tea House, Baranh and Ambarsariya — established Lahore restaurants carrying 3,800–18,000 reviews. **There is no wrong-country failure here at all.** The query string names the tile and the city, and Maps honours it.

**The sharper evidence is that Lahore is not being suppressed relative to Islamabad.** The 45%-vs-13% WhatsApp gap invited the theory that a non-PK IP was degrading Lahore results. But Lahore × food returned **71.5 businesses per query against Islamabad × salon's 66.3** — Lahore out-yields Islamabad on the same connection the moment the category changes. A geo-ranking penalty that reverses when you ask about restaurants instead of salons is not a geo-ranking penalty.

Adding the third enriched slice, the confirmation rates read **Islamabad × salon 45%, Lahore × salon 13%, Lahore × food 7%** (businesses with ≥1 confirmed number, as a share of domains crawled). Two Lahore slices bracket each other and Islamabad is the outlier — which is what §5.2 already suspected when it noted that "the Islamabad salon set skews to clinics and spas with real websites."

**What this does and does not license.**

- It **does** mean the existing data is good enough to run §16's validation pass on. The businesses are real, correctly located and correctly numbered; hand-checking 50 of them measures the scoring weights, which is what that pass is for.
- It **does not** close the A/B. Nobody has run the same slice through a PK proxy, so whether a PK IP surfaces a *different or larger* set — ordering and completeness, not country — is still unmeasured. That question is now the proxy's only remaining job, and it is a tuning question rather than a correctness one.
- The proxy therefore moves from **blocking** to **worth doing before scaling the fan-out**. Revise this row if the A/B ever shows a material difference.

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

**Note — revised by measurement, Aug 2026 (Phase 4).** Built as `services/dedupe.py` and run against both enriched runs. Two of the four tiers above are destructive as written, and the correction is one line: **`place_id` merges on its own; every other tier must also pass the 150 m distance test.**

*Scope: within one run.* The section does not say, so this is a decision. A `businesses` row belongs to exactly one run (`run_id NOT NULL`), so a cross-run merge has nowhere to put the survivor — whichever run owned it would be claiming a business the other one found, destroying the record §16's "validate by re-running" depends on. And `place_id` is unique *per run* precisely so a run can be repeated. Measured on the four Lahore × salon runs in the database: **232 place_ids collapse to 72, and three of the four overlap 100% with each other.** A cross-run merge would not be deduplicating a table, it would be deleting three runs. The operator's real want — one table, not four — is a **read-side** concern for Phase 5: the results view can union runs and collapse on `place_id` at query time without destroying anything.

**Note — the read-side union landed in Phase 5, Aug 2026, as this note proposed.** `services/results.py` takes a set of runs and, with `collapse=true`, folds rows sharing a `place_id` at query time. Measured on the four Lahore × salon runs: **232 rows collapse to 72**, exactly the figure predicted below, and **no run is modified** — §16's "validate by re-running" still has four runs to compare. Two rules the read side needs that the write side never had to answer: the winner is the best-scored row, then the most recently scraped, because the run that *enriched* a business knows more about it than the run that only discovered it (§10.2 measured that gap as 22 qualified against 0); and a business with **no** `place_id` is always kept, since collapsing rows that share nothing would be a merge asserted on no evidence — this section's own failure mode, arriving through the read path.

*Tier 1 (exact phone) and tier 4 (domain) merge chains into single rows.* Across both runs, **36 groups of businesses share a phone number and 7 share a domain, and not one of them is a duplicate:**

| Shared key | Businesses | Apart | What it actually is |
|---|---|---|---|
| 7 numbers + `houseofsalons.pk` | 3 | 171 m – 5.4 km | House of Salons F-7 Female, F-7 Men's, F-10 |
| 9 numbers + `royli.com` | 2 | 8.1 km | Royli Salon, two Islamabad branches |
| 7 numbers + `cosmosalon.pk` | 2 | 4.9 km | COSMO Salon, Gulberg and DHA |
| 4 numbers + `shelbysandco.com` | 2 | 13.3 km | Shelby's & Co., Johar Town and DHA |
| 2 numbers + `bellacaresalon.com.pk` | 2 | 10.1 km | Bella Care, Johar Town and Gulberg |
| 1 mobile | 2 | 40 m | Naveeds Salon and Nauman's Hair Saloon — unrelated |
| 1 mobile | 2 | 16 m | Spanish Club and a massage centre, same mall — unrelated |

Applied literally, tier 1 would have merged **11 Islamabad businesses and 7 Lahore ones** out of existence, each one a contactable branch with its own address. This is §10.1's own warning about false merges, arriving through the tier the section lists *first*.

*The distance test is the term carrying the discrimination.* Within 150 m, the highest name similarity between businesses sharing a number is **54.5** (Naveeds / Nauman's) and all such pairs are distinct businesses. Beyond 150 m, several score ≥ 88 (COSMO 100.0, Huma's 90.0, Bella Care 89.7) and every one of them is a separate branch. So phone and domain become **corroborating evidence that lowers the name bar** — `dedupe_corroborated_threshold`, default 75, which clears the measured 54.5 ceiling with room — and never waive the geo test. Tier 3 is unchanged and correct as specified.

*Name similarity cannot see a segment split, and one live merge proved it.* The first real merge this stage produced was **"Lavish Women Salon DHA Branch" into "Lavish Men's Salon Dha Branch"** — same domain, **3 m apart**, token-set ratio **93.1**. That clears even the strict 88 threshold, because a single differing token barely moves the ratio in an otherwise identical name. They are two separately-staffed premises with a number each, and §4.2's own salon synonyms already list "men's salon" and "ladies salon" as different queries. So a merge is refused outright when both names declare a clientele and the declarations differ — men/gents/barber vs women/ladies vs kids (`textnorm.conflicting_segments`). It is checked *before* the ratio and it is deliberately conservative: it fires only when **both** names say something, so a barber shop beside an unlabelled salon is not a conflict. Across the six runs in the database it fires 4 times.

*Within a run there is currently nothing left to merge.* Ingest already collapses on `place_id` at write time, and the cascade then produces **0 merges from 1,076 Islamabad candidate pairs and 41 Lahore ones**. That is the honest result, not a bug: this stage's value today is preventing bad merges, and its value for finding real ones is prospective — it arrives with the sources that do not share Maps' `place_id` (§5.3 directories, §5.4 verticals, §3.2 seed rows).

> **Update — the fuzzy tier's prospective value arrived, and it was zero. Aug 2026 (Phase 6).** The paragraph above says tier 3's value for *finding* duplicates "arrives with the sources that do not share Maps' `place_id` (§5.3 directories, §5.4 verticals, §3.2 seed rows)". §5.3's directories arrived. Over **333 BusinessList listings against 726 businesses in four slices, `fuzzy_name_geo` matched 0.**
>
> The tier is not mis-tuned; the input is. **BusinessList publishes geocoded approximations rather than surveyed positions**, so its coordinates miss by hundreds of metres routinely and by thousands of kilometres occasionally — "Dilara's Salon" and "Dilara Salon" are the same business 391 m apart. The 150 m radius in tier 3 was calibrated Maps-against-Maps, where both sides come from *one* source's survey. Across sources with different coordinate provenance it is measuring their disagreement about where a business is, not the distance between two businesses.
>
> **Widening it was tested and does not help** — at 500 m the extra recall is one pair, and that pair is a false match ("Hair and Hair" against "Ashley's Hair and Makeup Studio", which clears 88 because a short name is a token-subset of a long one). The §5.3 note has the full table. The threshold stays at 150 m.
>
> The generalisable rule: **a distance test is only as good as the worse of the two coordinate sources**, and a cross-source join should say which source it trusts for position. Where a source ships Maps' own `place_id` — §5.4's PakPlay embeds it in the venue-page iframe — tier 2 applies and none of this arises.

> **Update — the first real merge, Aug 2026 (Lahore × food, 429 businesses).** At seven times the scale the cascade was tuned against, it found one: **"Utopian Chinese"**, merged on the `exact_phone` tier *with* the 150 m distance test, absorbing one duplicate row. The rejection counts are the part worth reading, because they are what the Phase 4 correction was for — from **936 fuzzy name-geo candidates, 110 exact-phone and 65 domain**, it rejected **935 on name similarity and 92 on distance** and merged exactly one. Applied as §10.1 originally specified, the phone and domain tiers alone would have merged well over a hundred rows on a category where shared reservation lines and restaurant groups are routine. The demotion of those tiers to corroboration holds up at scale, and the stage now has a real positive to its name rather than only a record of harm prevented.

*One number can legitimately sit on two rows after a merge.* Each is a real provenance record with its own `source`/`source_url`, and folding them would drop whichever source lost — which is the input `source_agreement` counts. So the merge re-parents every contact row unchanged, and §3.3 ranking gives the export slot to one of them (see the §3.3 note). "Never discard a contact" is applied literally.

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

**Note — measured, Aug 2026 (Phase 4).** Built as `core/scoring.py` (pure) and `services/scoring.py` (Stage 5), and run against both enriched runs. The weights are unchanged; what needed deciding was how each term behaves when its input is absent, which §5.1 had already flagged as load-bearing and which turned out to be more load-bearing than expected.

**Missing is not zero, and the line runs between two kinds of missing.** A term is *omitted* — dropped from the numerator **and the denominator**, with the score renormalised over the weight that actually applied — when the underlying fact plainly exists and only our observation of it failed. A term is *scored 0* when "we found nothing" is itself a true statement about the record.

| Term | When absent | Why |
|---|---|---|
| `business_signal` | **Omitted** | Every business has some real level of popularity; Maps declining to publish it is our gap |
| `person_attribution` | **Scored 0** | Most PK SMB salons genuinely have no publicly named owner — that is a fact about the record, not a hole in it |
| `whatsapp_evidence`, `contact_confidence` | **Scored 0** | We looked and found no contact. 25 of the 199 Islamabad businesses are in this state and they are correctly bad leads |

**Omission is partial where the inputs are, and getting this wrong inverts §5.1's bias rather than removing it.** `business_signal` has two inputs that go missing independently: `review_count` is present on 80% of the Islamabad run and **0% of the Lahore one**. Scoring the gap as zero reviews would have sunk the entire Lahore run, exactly as §5.1 warned. But scoring rating alone at *full* weight overshoots the other way, because PK salon ratings cluster hard at the top (Islamabad median 4.6, p90 5.0 — so rating alone normalises to ~0.90) while review counts spread widely (median 31 → ~0.65, so rating-and-reviews normalises to ~0.78). Measured: at full weight the Lahore run's mean score came out **1.8 points above** where the same businesses belong. So one input present carries **half** the term's weight and the rest renormalises away. The two runs then agree to within a point on comparable rows.

The corollary is worth stating on its own: **`rating` barely discriminates, and `review_count` carries nearly all of `business_signal`.** A run that arrives without review counts has lost most of that term's value, and no weighting recovers it. That is a §5.1 payload-richness problem, not a scoring one.

**`completeness` must not price in the shape of the market.** Defined as the populated ratio of four fields — `address`, an online presence, an email, a second distinct number — chosen against measured fill rates. Two consequences:

- **Having a website is deliberately not one of them.** Only 32% of discovered businesses have one; scoring it directly would dock two thirds of every run for a property of PK SMB hosting. It sits inside a disjunction with the Facebook and Instagram columns, so a salon reachable only on Instagram scores the same as one with a domain.
- `area` (100% filled) and lat/lng (100%) are excluded. A field that is always present adds a constant to every row and buys no discrimination.

**`source_agreement` is counted per business, not per number.** §5.2 measured that only 19 of 53 confirmed numbers were ones Maps also carried, so per-number agreement is too rare to be a signal. Per business it is real: **43 of 199 Islamabad and 21 of 60 Lahore** businesses carry contacts from both `google_maps` and `business_website`.

**The practical ceiling is 85, and the run says so.** §8's attribution engine is Phase 9, so the 15-point person term is 0 for all but **1 business in 199**. The other five weights are *not* inflated to compensate: every §16 weight tuned against an inflated scale would have to be re-tuned the day Phase 9 lands. Instead each run reports `unattributed_ceiling: 85` so nobody reads a table that stops at 85 as a fact about the businesses.

**Measured output.** Both runs, `owner_first`:

| | Islamabad × salon | Lahore × salon |
|---|---|---|
| Businesses scored | 199 | 60 |
| Mean / median score | 46.4 / 49 | 54.3 / 51 |
| p10 / p90 / max | 9 / 80 / 97 | 32 / 72 / 82 |
| Mean, businesses with a phone | 51.9 | 54.3 |
| Mean, businesses with a `confirmed` number | 79.5 | 78.2 |
| **Qualified (≥ 60 + a mobile)** | **45 (23%)** | **22 (37%)** |

Two things to read from this. The cross-run mean gap is almost entirely the 25 Islamabad businesses with no phone at all (p10 of 9 against Lahore's 32); conditioned on being contactable the runs are 2.4 points apart. And **`confirmed`-WhatsApp businesses land at ~79 in both runs independently**, which is the check that the scale means the same thing across slices — it is what makes §16's weight tuning transferable.

**Scoring also prices §5.2, and the number is stark.** The database holds three Lahore × salon runs that were discovered but never enriched, alongside the one that was. Same city, same category, same discovery code:

| Lahore × salon | Businesses | Mean score | **Qualified** |
|---|---|---|---|
| Discovery only ×3 | 52–60 | 46.4 – 48.2 | **0, 0, 0** |
| Discovery + §5.2 enrichment | 60 | 54.3 | **22** |

A discovery-only run produces **no qualified leads at all**, because every number in it is a §9.3 *likely* at 0.60 and nothing else in the record can lift a business over 60. That is not a scoring artefact — it is the honest statement that a Maps listing alone is not a qualified lead. It also means §16's "ship after Phase 5" bundle is doing the right work: Stage 2 is not a 10% uplift on this scale, it is the difference between a table and an empty filter.

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

**Tables this SQL does not list but the build added.** `do_not_contact` (§15 requires it in v1 and warns that retrofitting it is painful) and `source_state` (§7's circuit breaker, surfaced as Screen 2's pills) have been present since Phase 1. `extractions` was added in Aug 2026 for §13 Screen 3's Extract control — one row per business handed out, `UNIQUE(business_id)`, cascading from both `businesses` and `runs`, holding the numbers **as sent** rather than as they now stand. It is a working queue, not a compliance record; `do_not_contact` remains the only table that makes a removal stick.

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

**Note — built, Aug 2026 (Phase 5).** `export/` (columns, row projection, CSV writer) and `services/results.py` (the query layer). All four bullets hold; what needed deciding was the architecture around them.

**"Respect the active filters" is an architectural requirement, not a behavioural one.** It makes any divergence between the table and the CSV a defect *by definition*, so the two cannot be allowed to be separate code paths that happen to agree. There is therefore **one** `ResultQuery`, **one** `fetch_results`, and **one** FastAPI filter dependency shared by `GET /api/results` and `GET /api/runs/{id}/export.csv`. The frontend's Export button reuses the exact query string the table is displaying. `test_export_and_table_are_the_same_rows` and `test_export_respects_the_same_filters_as_the_table` are the tests that fail if anyone gives the exporter its own parsing.

**The Excel armour lives only in the CSV writer.** The row projection emits clean values — `None` for missing, a plain `+923001234567` for a phone — and the writer decorates. That split is what lets the JSON table and the CSV be *the same data by construction*; a projection that emitted `="+92…"` would put a formula on screen.

**Filtering below the run level happens in Python, deliberately.** The §13 contact-level filters (WhatsApp status, phone type, source) are predicates over the *exported* contact set, which is `rank` + §15 suppression + §12.1's 4-slot cap taken together. Expressing that in SQL means reimplementing §12.1 in a second language where it can rot independently. Run scoping and the score floor are pushed into SQL so what is materialised stays bounded by one run — the largest is 199 businesses. Revisit if a run ever reaches five figures.

**Three things the projection refuses to do**, each a rule from elsewhere in this document arriving at the last mile:

- **A blank is never a zero.** `review_count` is absent on 100% of the Lahore run; exporting it as `0` would tell the operator every salon in Lahore has no reviews. §10.2's load-bearing rule does not stop at the scorer, and it does not stop at a placeholder either — a `-` or a `None` in a cell gets re-imported as data by whatever reads the file.
- **`contact_person`, `contact_role` and `attribution` come from one contact.** A name from one number and a role from another is a join we never made (§8).
- **`phone_count` counts distinct numbers, not provenance rows, and is uncapped.** The 4-slot cap is on columns; §10.1 never discards a contact, so a business with 7 numbers exports `phone_count = 7` and the operator can see the four shown are not all of them.

**Measured, both enriched runs.** Islamabad × salon at `min_score=60` exports 41 columns × 45 rows, 31 KB, BOM intact, phones armoured, **6 rows with a blank `review_count`** and non-ASCII business names surviving the round trip. Lahore × salon exports 22.

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

**Note — resolved by measurement, Aug 2026 (Phase 5). The two halves of that line are different kinds of question, and only one of them is ours to answer.**

§5.2 forbids the second number outright — *"Measure per slice; do not extrapolate one run's confirmation rate into the §13 estimated-available figure"* — but the mockup shows one, and that tension had to be resolved rather than split the difference. The resolution:

- **Runtime is ours.** It falls out of the query plan and our own §7 pacing. Measured from this installation's live discovery runs and shown as a **range**, tagged with its basis (`measured` / `measured_single_run` / `doc_projection`).
- **Availability is the market's.** How many salons exist in Faisalabad is a fact about Faisalabad. It is reported **only where this exact city × category has been run before**, as that run's actual outcome; otherwise the screen reads **"no basis · never run"**.

**The measurement that settles it.** Unique businesses per Maps query, same category, three cities:

| Islamabad × salon | Lahore × salon | Karachi × salon |
|---|---|---|
| 66 / query | 20 / query | 19.5 / query |

A **3.4× spread inside one category**. Any single multiplier is wrong for two of the three cities. Worse for anyone hoping to fit a curve: the two Lahore runs disagree in the *wrong direction* — 3 queries returned 60 unique and 6 queries returned 52 — so the data does not support a monotonic yield model, let alone a point estimate. Prior measured outcomes are therefore reported **unscaled**, with a caveat naming the query count they were measured at, because §14 also measured a 67% duplicate rate across near-synonyms: unique yield saturates rather than growing with the plan.

> **Correction — Lahore × food, Aug 2026.** The table above is all one category, so it was read as a *city* effect. A live 6-query run of **Lahore × food returned 429 unique businesses — 71.5 per query**, which is **higher than Islamabad × salon's 66.3**. So the spread is at least as much a **category** effect as a city one: within Lahore alone, food out-yields salon **3.6×** (71.5 against 20), on one IP, one day, one codebase. The two axes are comparable in size and they compound. This does not weaken the refusal — it widens it. An estimate extrapolated from a slice differing in *either* city or category is unsupported, which is why `estimate_run` keys on the exact pair and nothing looser.

**A second refusal, for a different reason.** Where a slice has prior runs that were never enriched, the business count is real but the *qualified* count is structurally 0 (§10.2: three discovery-only Lahore runs at 0 against the enriched one's 22). Publishing that 0 as a forecast would read as "this city has no leads" rather than "that run was not finished", so it is withheld and the reason is stated.

**Stage timing did not exist and now does.** The estimator had nothing to measure against: the six runs in the database carry only a run-level wall clock that includes every re-run of every stage, and three of them were cache hits that finished in under half a second. `jobs.finish_stage` now writes `elapsed_seconds` into each stage's report in `runs.stats`, so this gets more honest with every run rather than staying pinned to §14's published constants.

### Screen 2 — Run Progress

Per-stage counters (discovered / enriched / attributed / deduped / qualified), a live log tail, per-source status pills (`ok` / `throttled` / `blocked`), and a Cancel button. Results stream into the table as they qualify — don't make the operator wait for the full run.

**Note — built, Aug 2026 (Phase 5).** Three corrections to what this section assumed was already available.

**The status pills had a table and no writer.** `source_state` has existed since Phase 1 and **nothing had ever written a row** — `core/pacing.BreakerRegistry` is in-process and dies with the worker, so its `statuses()` was unreachable from the API. `jobs.persist_source_state` now projects each stage's report onto `source_state` when the stage completes. It preserves §5.2's distinction rather than flattening it: **a few refusals are `throttled`, only our egress being blocked is `blocked`** — conflating those once cost a live run 19 healthy domains. Verified on the Lahore run, whose 2 refused hosts surface as `throttled`, not `blocked`.

**The counters read `runs.stats`, not a second counter path.** Every stage already writes its report there under its own key, and the CLI run summary reads the same place — so the screen and the console cannot report different numbers for one run. The job wrapper *merges* timing into that report rather than replacing it, or the counters Phases 2–4 produce would be destroyed on completion.

**Cancel stops the pipeline at the next stage boundary, and the UI says so.** It drops every queued stage immediately and marks the run cancelled, so the next stage refuses to start. **A stage already executing runs to completion** — killing a worker mid-Playwright would leave the §7 cache and the breaker state inconsistent, and the longest stage here is minutes. Because stages chain themselves (§2) there is at most one job in flight per run, so "the current stage, then stop" is the entire behaviour and can be stated in one line on screen rather than implying an instant halt.

**One bug this screen found.** A single-stage re-run (`chain=False`) left the run at `running` for ever, because nothing downstream was coming to close it — Screen 2 showed a spinner on a run that had finished seconds earlier. That is the mirror image of §5.5's rule and just as misleading, so a non-chaining stage now brings the run to a terminal status itself.

### Screen 3 — Results Table

- Sticky header, virtualised rows (TanStack Table + `@tanstack/react-virtual`)
- Sort any column; default `lead_score DESC`
- Filters: WhatsApp status, has-owner-name, min score, phone type, source, free-text
- Column visibility toggle, persisted to `localStorage`
- Green badge on `confirmed` WhatsApp cells, grey on `likely`
- Row expand → evidence panel with source URLs
- **Export CSV** button, top right, exports the current filtered view
- Bulk select → delete (for honouring removal requests)

**Added, Aug 2026 — the website split and the extraction queue.** Two controls this section did not ask for, both driven by how the table is actually worked rather than by the spec:

- **Website filter — `any` / `has a website` / `no website on record`.** Not cosmetic: the two halves are different work. A business with a site is one §5.2 can raise to `confirmed`; a business without one is a business where §6's social pass is the only route to a `confirmed` label that will ever exist, and §6.8 measured 97 businesses across the seven runs holding a social URL and no website at all. The filter is exhaustive by construction — a blank `website` counts as *no website*, because the column is gap-filled from §5.1's payload and §6.4's bio link and a surviving empty string is the absence of a site. The label says *on record* rather than *has none*: nothing here proves the negative, which is §10.2's "missing is not zero" stated for a filter.

- **Extract → top 30 / 50 / 100.** Copies every `confirmed` and `likely` number off the top of *the currently filtered, currently sorted view*, one per line, and writes those businesses to an extraction ledger so the next pull moves past them. The operator's loop is not "export 429 rows" — it is "give me the next 30 worth messaging, and not the same 30 tomorrow", and nothing in §12 expresses that.

  Five rules, each of which is an existing rule of this document applied one output further along:

  - **The pull is the table.** It reads the same `ResultQuery` through the same `fetch_results` as the screen and §12.2's CSV. §12.2 makes a CSV that disagrees with the table a defect by definition; a clipboard that disagreed would be the same defect wearing a different button.
  - **`confirmed` and `likely` only, from the label.** §9.3's raw evidence score stays internal, and `no` is the one label the public record argues *against*. Nothing in this path reaches the network — §9.3's standing rule about never probing WhatsApp is unaffected.
  - **Every qualifying number, not §12.1's four.** The 4-slot cap is a property of a *column set*; §10.1 forbids it becoming a property of the data.
  - **A row with no qualifying number still counts and is still marked.** The batch size counts businesses *worked*, not numbers found. A row looked at and found wanting would otherwise be re-offered on every pull for ever. The count comes back as `without_numbers` and the screen states it, because a clipboard shorter than the batch has to be explained rather than inferred.
  - **Marking is not §15.** `do_not_contact` says "never contact this"; the ledger says "already sent". Clearing an entry — one, a run's worth, or all — puts the business back in the queue and deletes nothing else. The two lists are never read or written from each other's code.

  The mark is a decoration on the row, never a filter: hiding extracted rows would shrink the CSV with them, and a business would vanish from an export because somebody once copied its number. §11 gains an `extractions` table for it (migration `c7f1a4be2d19`), storing the numbers **as sent** rather than as they now stand — a later run raising a contact's §9.3 evidence must not rewrite the record of a message already gone out.

**Measured on all seven live runs, Aug 2026.** The split is exhaustive on every one of them (`has_website` true + false = the unfiltered total, asserted rather than eyeballed), and website coverage varies enough to matter before you filter on it: **34% on Islamabad × salon against 56% on Lahore × food**. Two findings justify the rules above rather than merely illustrating them.

**Yield decays down the table, sharply, which is why a barren row is still marked.** The first 30 of Lahore × salon are 30 businesses carrying 30 numbers and **0 without**; the *second* 30 are 17 numbers and **13 without**. Karachi × salon is starker — its second pull marked 9 businesses and produced **no numbers at all**. That is §3.3's ranking working as specified, and it is the whole argument for the ledger: without it, every pull after the first re-serves the same dead tail, and the operator has no way to tell that they have already seen it.

**The split corroborates §5.2 being the confirmation engine, from the read side.** Filtering Lahore × food to `whatsapp=confirmed` returns 20 rows, **19 of which have a website and 1 of which does not** — the same claim §5.2 makes from the write side, arrived at by a different route.

**Note — built, Aug 2026 (Phase 5).** Next.js 16 + TanStack Table + `@tanstack/react-virtual`, per §2's stack. Everything above is implemented; four things are worth recording.

**Sorting and filtering are server-side, and TanStack's client-side models are deliberately unused.** §12.2 requires the CSV to be the filtered, *sorted* set on screen. If the table sorted in the browser, the Export button would produce a differently-ordered file from the view it was clicked on — the divergence §12.2 exists to prevent, arriving through the sort rather than the filter. So filter state is a query string that both the table and the export read, and TanStack does what it is uniquely good at here: column definitions, visibility state, virtualisation.

**Virtualised with spacer rows, not absolute positioning.** The usual `translateY` approach needs flex rows with fixed widths, which loses the `<thead>`/`<tbody>` column alignment that a real table gives for free — and this section asks for a *sticky header over sortable columns*, so that alignment is the feature, not a detail.

**The evidence panel is a drawer rather than an inline expanded row.** A variable-height row inside a virtualised body throws off the spacer arithmetic that keeps the scrollbar honest. This section asks for an evidence panel, not specifically an inline one.

**The empty table explains itself, because the most common empty table here is not an error.** §10.2 measured that a discovery-only run has **0 qualified leads by construction**, and four such runs are in the database. A bare "no results" under `min_score ≥ 60` would read as a broken filter; the screen instead says the §5.2 website pass has not run and points at the button that runs it.

### Settings

Proxy endpoint + credentials · per-source rate limits and daily budgets · SERP/Meta API keys · cache TTL · dedupe fuzzy threshold · concurrency · default city list.

**Note — built read-only, Aug 2026 (Phase 5).** Everything listed already resolves through `config.Settings`, which is the codebase's single reader of `os.environ`. A settings *write* path would introduce a second source of truth that could disagree with the `.env` file on disk, and for a single-operator tool "edit `.env` and restart" is both honest and shorter than the code that would replace it. Secrets are reported as booleans and never echoed.

The screen carries two things beyond the list above, both of which exist to make an invisible state visible: **whether anything is actually consuming the queues** (with no worker and no `QUEUE_SYNC`, a created run sits at `queued` for ever and looks like a hang rather than a missing process), and **the per-slice WhatsApp confirmation spread** — so the 45%/13% gap that makes Screen 1 refuse to estimate availability is something the operator can see rather than take on trust.

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

> **Qualified rate measured, Aug 2026 (Phase 4) — and the table's last row is optimistic.** This table projects ~380 qualified from ~600 with a phone, a **63%** conversion. Scored against the real runs (§10.2's note), the actual rate is **26%** of contactable Islamabad businesses and **37%** of Lahore's. Two caveats before this is treated as a shortfall: §8's attribution engine is Phase 9, so every row is scored against an 85-point ceiling rather than 100, and the ≥ 60 bar was never calibrated against ground truth — that is precisely what §16's validation pass is for. The honest reading today is that **the constraint on a run's output is the qualification rate, not the volume**: 199 businesses from 3 queries already clears the raw target, and 45 of them qualify. Re-check this row after §16's hand-check, and re-tune the bar or the weights rather than the fan-out.

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

**Note — built, Aug 2026 (Phase 5).** The table had existed since Phase 1 and **nothing had ever queried it**. Two decisions.

**Deleting is not removing, and the suppression entry is the durable half.** This is the whole design of the bulk-delete path. Deleting a business row honours a removal request right up until the next run rediscovers the same salon from the same Maps listing and puts it straight back — which makes row deletion the *cosmetic* half of the operation and `do_not_contact` the part that satisfies "survives re-runs". So `POST /api/do-not-contact/bulk-delete` **suppresses first, then deletes, in one transaction**: every number on the selected businesses plus the registrable domain where there is one. Deleting without suppressing is still possible, because clearing a test run is a real need, but it returns an explicit warning saying the rows will come back — it is never silent.

**Checked on every read, not only at export.** This section says "checked at export time"; that is the minimum and it is not enough on its own. A suppressed number that still renders as `phone_1` on §13 Screen 3 is a number the operator dials. And since §12.2 requires the file to match the screen anyway, applying suppression anywhere *other* than the shared query layer would create exactly the divergence that section forbids. So it lives in `services/results.py` and both endpoints inherit it. Three rules fell out of doing it there:

- A suppressed **number** removes that number from the ranked slots.
- A suppressed **domain** removes the whole business — that is the business itself asking, not one line being retired.
- A business whose numbers are *all* suppressed leaves the table: there is nothing left to ring. A business that never had a phone is a different fact and stays — 25 of the 199 Islamabad businesses are in that state and they are bad leads, not removal requests.

**The rows are never destroyed by suppression, and the counts are never hidden.** An excluded contact keeps its row (§10.1 never discards a contact; this section needs it for provenance), and every response reports `suppressed_contacts` / `suppressed_businesses` — carried on the CSV as `X-Leads-Suppressed-*` headers too. §15 applied silently is §15 nobody trusts, and an operator seeing 44 rows where a colleague saw 45 needs to know a suppression did that rather than a bug.

---

## 16. Build order

| Phase | Deliverable | Est. |
|---|---|---|
| **1** | Schema, queue skeleton, raw-fetch cache, phone normaliser + PK classifier | 3–4 d |
| **2** | Google Maps module (grid fan-out, network interception, detail panels) | 5–7 d |
| **3** | Website module (wa.me, widgets, tel:, JSON-LD) | 2–3 d |
| **4** | Dedupe + scoring + ranking by `number_preference` | 2–3 d |
| **5** ✅ | Frontend: run form, progress, table, CSV export | 4–5 d |
| **6** ✅ | Directory modules (BusinessList, UrduPoint) — **built; measured at zero yield, see §5.3** | 2–3 d |
| **7** | Vertical modules (PakPlay, Turfy, Zameen click-reveal, Foodpanda) | 4–5 d |
| **8** ✅ | FB/IG **Tier 3 only** — rendered, not fetched. Tier 2 measured and deferred, see §6.7 | 3–4 d |
| **9** | Person attribution engine | 3–4 d |
| **10** | Meta API cutover (start review at Phase 1) | 1–2 d + review wait |

**Ship after Phase 5.** That's Maps + websites + a working table with export — roughly 80% of the value, and it tells you whether the real hit rate matches §14 before you invest in the long tail.

**Note — Phase 5 complete, Aug 2026. This is the ship gate, and it is reached.** Delivered: §12.1's 41-column projection and §12.2's CSV export; a FastAPI app (run create/list/detail/cancel, single-stage re-run, preference change, results table, both export endpoints, §15's compliance routes, and the §13 pickers reading from `taxonomy` so §4.2's dictionary has one definition); §2's queue given producers, a consumer and a worker entrypoint; and §13's three screens plus Settings in Next.js. §15's suppression check and bulk-delete path landed here too, and §10.1's cross-run union with them.

The "4–5 d" estimate was for **four separate deliverables plus two**, and the queue and the frontend are each comparable in size to the export work the row of this table names. Anyone re-planning from this table should read that row as four items, not one.

**What Phase 5 does not include.** §3.2's seed mode is still unimplemented and still unassigned to a phase, though §13 Screen 1 is now where it would surface. The §5.1 Maps detail-panel fallback for the ~13% of records with no phone in the payload is still unbuilt. Directories are accepted on the run form and warned about rather than refused, because they are additive to a Maps run — but they contribute nothing until Phase 6.

**The §16 validation pass below is now the immediate next thing, and the exporter is how you produce its sample.** A 50-row hand-check sample comes straight out of `GET /api/runs/{id}/export.csv`.

**Note — Phase 6 complete, Aug 2026, and it is the first phase that shipped a negative result.** §5.3's horizontal directories are built (`sources/businesslist.py`, `services/directories.py`) and measured across four live slices at **7 matches from 333 listings and 0 contacts added**. Three of §5.3's four sources were refused on recon and recorded in `taxonomy.EXCLUDED_SOURCES` — including **UrduPoint, which this table names in the Phase 6 row**: it holds 4% mobiles and 0% owner names over its 21 Lahore restaurant records, at 171 KB and one request each. So the row above is delivered, and what it delivered is the knowledge that this branch is not worth extending. §5.3 carries the full measurement and the two rejected remedies (widening the geo radius; inserting unmatched rows as discovery).

**The ordering question this raises, stated plainly.** §16's phase table puts directories at 6 and verticals at 7, and this section's "Validation before scaling" says to tune the weights against ground truth *before* building more source modules. Phase 6 was built ahead of that validation and returned nothing — which is weak evidence for the validation-first argument, and strong evidence for a narrower claim: **the binding constraint on a new source is whether its records can be joined to the ones we have.** Directories fail on the join, not on the data. §5.4's PakPlay ships Maps' own `place_id` in its venue-page iframe, so it joins through §10.1's tier 2 with no name ratio and no distance test. That is the property to select for when choosing what to build next.

**Note — Phase 8 complete, Aug 2026, and it is the first phase that reconned before it built.** §6 carried no correction notes at all, so `scripts/spike_social.py` measured every claim in it before a line of the module was written. §6.7 holds the numbers. Three of §6's factual claims about the markup were wrong and the fourth — the one that matters — was right: the number really is one hop away on a page with no wall, but the page must be **rendered**, not fetched.

**What this row delivered, against what it promises.** The table above says "Tiers 2–3". Only **Tier 3** was built (`sources/social.py`, `services/social.py`), and Facebook is read before Instagram, which is the reverse of §6's own ordering — §6.7 measured 58% of rendered FB Pages carrying a WhatsApp button against 10% of IG profiles, and this stage exists to produce `confirmed`.

**Tier 2 was measured and deliberately deferred, which is different from unbuilt.** A targeted `site:instagram.com "<name>" <city>` returns the right profile **6 times in 20**, and a name-similarity join is unusable for filtering it — 11 of 20 top hits score ≥88 while only 6 are the right profile, because `instagram.com/popular/<slug>` and `/p/` permalinks carry the business name too. A bare-handle-only filter gets 6 correct out of 7 accepted at 30% recall. That is a workable feeder, but §6.7 also found a free one: **6 of 12 Facebook Pages link their own Instagram account**, needing no credits, no name matching and no join test. Ship Tier 3 against the social URLs already held, measure what it yields, and revisit Tier 2 with that number in hand. The Serper key and its 2,500 credits are in place either way.

**§6.4's bio-hub branch does not exist in this market.** §6.4 says a bio link is "virtually always" a Linktree/beacons.ai hub carrying a WhatsApp button, and builds its whole pipeline diagram around that. Across 32 rendered profiles, **zero** bio links were a link-in-bio hub. The real distribution is stores (15), other social profiles (8), `wa.me` (2), nothing (7) — so no hub-follower was built, and a store bio-link gap-fills `business.website` for §5.2 to pick up instead. This is the §5.3 lesson in a second place: the branch a section spends its prose on is not always the branch the data uses.

**Tier 4 (§6.5, the operator queue) was not built** and is not started. It is UI work, capped at 20 per run, and `operator_queue_cap` has been in `config.Settings` since Phase 1 with no reader.

**What it yielded, live.** §6.8 has the table; the short version is that businesses holding a `confirmed` WhatsApp number went **4 → 9** on Lahore × salon, **13 → 20** on Lahore × food and **28 → 30** on Islamabad × salon, plus 36 websites gap-filled for §5.2 to crawl on a later pass. This is the first source module since §5.2 to add anything, and unlike Phase 6 it was ordered by a measurement rather than by the table above. Four defects were found *by running it* — a double-encoded Facebook link shim, a breaker that treated an ordinary empty Page as a broken selector, a WhatsApp number left unclassified and therefore unqualifiable, and a cached shell that would have blocked its own retry for 30 days. Each is now pinned by a test; three of them failed silently, which is the §5.5 pattern one layer below where §5.5 expects it.

### Validation before scaling

Run **Lahore × salon** and **Lahore × food**, hand-check 50 random rows:

- Is the phone correct and reachable?
- Is `phone_1` genuinely the best number under the chosen preference?
- Do `confirmed` WhatsApp labels hold up when dialled?
- What's the true duplicate rate after merge?

Tune the scoring weights against that ground truth before building more source modules. Everything downstream depends on those weights being right.