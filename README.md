# Pakistan Local Business Lead Scraper

Discovers local businesses by city + category and extracts contactable numbers,
prioritising WhatsApp-capable mobiles attributed to a named owner where the
public record supports it.

**The design document is [implementation.md](implementation.md).** It is the
source of truth — the code references it by section (§5.1, §9.2, …), and any
disagreement between the two should be resolved by fixing one of them, not by
ignoring it.

## Status

Phases 1–6 of the §16 build order are complete. **§16's ship gate is reached.**
Phases 7–10 are outstanding.

| | |
|---|---|
| ✅ Phase 1 | Schema + migrations, queue skeleton, raw-fetch cache, phone normaliser + PK classifier, WhatsApp evidence scorer, proxy abstraction, taxonomy/synonym config |
| ✅ Phase 2 | Google Maps discovery — grid × synonym fan-out, payload interception, scroll to exhaustion, pacing, circuit breaker, cross-query dedupe |
| ✅ Phase 3 | Business website module — wa.me, chat widgets, `tel:`, JSON-LD, emails, socials; 4-page crawl budget, no browser |
| ✅ Phase 4 | §10.2 lead scoring, §3.3 ranking by `number_preference`, §10.1 dedupe cascade |
| ✅ Phase 5 | §12 CSV export, FastAPI, the §2 queue given producers + a worker, §13's three screens + Settings, §15 suppression + bulk delete, §10.1's cross-run union |
| ✅ Phase 6 | §5.3 horizontal directories — BusinessList.pk built, three sources refused on recon. **Measured at zero yield; defaulted off** ([§5.3](implementation.md)) |
| ✅ Phase 8 | §6 Tier 3 — FB Pages and IG profiles, **rendered** logged out. Facebook first, against §6's own ordering. Tier 2 measured and deferred ([§6.7](implementation.md)) |
| ⬜ Next | **§16 validation pass — needs a human.** Hand-check 50 rows, then tune the weights |

### Phase 8: §6 was never measured, and three of its four claims were wrong

§6 was the only section of the design doc carrying **no correction notes at
all** — every figure in it was written before anything was fetched. Phase 6 had
just shown what that is worth, so Phase 8 reconned first
([`spike_social.py`](backend/scripts/spike_social.py)) and wrote the numbers into
§6.7 before writing the module.

**§6.4's mechanism is wrong; its substance is right.** The phone really is one
hop away on a page with no wall — but the page has to be *rendered*:

| | Plain fetch (what §6.4 specifies) | Rendered, logged out |
|---|---|---|
| IG bio text | **0 / 20** | **20 / 20** |
| IG bio link | **0 / 20** | 14 / 20 |
| FB Page reachable | **0 / 20 — HTTP 400** | **12 / 12 — HTTP 200** |

A logged-out `httpx` GET of an Instagram profile returns 200 and ~605 KB of JS
shell whose `<title>` is the word `Instagram`. A Facebook Page returns HTTP 400
and a 1.5 KB error page — not a login wall — for every URL variant. Both render
fine in an ordinary logged-out browser. That is a **cost** change, not a policy
one: no login, no credential store, no cookie injection, no fingerprint work,
which is exactly where §6.1 draws the line. Budget ~20s per business.

**Two corrections that decide whether the tier works at all.** §6.4 says to read
the bio from `og:description`; on Instagram that tag holds *"12K Followers, 10
Following, 228 Posts"* and the bio is in `<meta name="description">`. Reading the
tag §6.4 names measures this tier at 0 numbers in 20 profiles when the real
figure is 10. And Meta hides these URLs inside JSON string literals — `\/` for
slashes, and Facebook **double-encodes** its outbound link shim as
`u=https%3A%2F%2F…`. The first live run silently truncated every
bio link to the five characters `https`, which looks identical to "this Page has
no bio link". Both are pinned by tests.

**Facebook is the confirmation engine, not Instagram** — the reverse of §6's and
§16's ordering:

| Rendered | Instagram (n=20) | Facebook (n=12) |
|---|---|---|
| Inline `03xx` in bio | **10/20 (50%)** | 1/12 (8%) |
| **WhatsApp button** | **2/20 (10%)** | **7/12 (58%)** |

§9.3 scores a platform WhatsApp button at 0.90 — `confirmed`. Instagram's bio
numbers are 0.60 *likely*, the same score 850 of 898 businesses already carry, so
they matter for the 47 businesses with no phone at all and are close to a
constant elsewhere. So Stage 3 reads Facebook first, and §6.6's one-request-
per-business cap means a business holding both URLs is read on Facebook only.

**What it did on three live slices** — the first source module since §5.2 to add
anything at all:

| | Lahore × salon | Lahore × food | Islamabad × salon |
|---|---|---|---|
| Businesses / with a social URL | 60 / 26 | 428 / 140 | 199 / 55 |
| **Businesses with a `confirmed` number** | 4 → **9** | 13 → **20** | 28 → **30** |
| **Qualified** (≥ 60 + a mobile) | 22 → **26** | 65 → **76** | 45 → **47** |
| Websites gap-filled for §5.2 | +3 | +28 | +5 |

**+17 qualified leads from 36 new contacts.** Islamabad is the informative slice:
it is the one §5.2 already did *best* on, and Tier 3 still added to it. The tiers
are not substitutes — a business publishes a WhatsApp button on its Page or it
does not, independently of whether it also runs a website. The gap-filled websites
matter because **97 businesses across the seven runs hold a social URL and no
website at all**, and for them this stage is the only route to a confirmed number
that will ever exist.

Read the +17 with the caveat below about the uncalibrated ≥ 60 bar: a `confirmed`
label is worth +12 points, which is the width of the 50–59 band the score
distribution piles into, so the bar is doing as much work here as the data is.

Re-running the stage changes **0 contacts** — verified, not assumed. The merge is
upgrade-only on evidence and gap-fill-only on everything else.

**The tier's real ceiling is the shell rate, not the button rate.** 31% of
profiles on Lahore × food and 62% on Islamabad × salon come back HTTP 200 with no
profile in them — real Pages, not junk URLs. It is transient (a retry rendered
one) and it got worse across a long session, which is the signature of soft
rate-limiting. Budget on rendering about two thirds of what you ask for per pass.
Shells are deliberately **not cached**: a 30-day TTL on a non-result would turn one
transient gate into a month of permanent misses. The cost is that a re-run retries
them (~40 requests on Lahore × food) rather than being free.

**§6.4's headline branch does not exist here.** §6.4 builds its pipeline around a
bio link that is "virtually always" a Linktree-style hub with a WhatsApp button.
Across 32 rendered profiles, **zero** were. The real distribution is stores (15),
other social profiles (8), `wa.me` (2), nothing (7) — so no hub-follower was
built. A store bio-link gap-fills `website` and §5.2 picks it up on the next
Stage 2 pass.

**Tier 2 (§6.3) was measured and deferred, which is not the same as unbuilt.** A
targeted `site:instagram.com "<name>" <city>` returns the right profile 6 times
in 20, and a name-similarity filter is unusable — 11 of 20 top hits score ≥88
while only 6 are correct, because `/popular/` location pages and `/p/` permalinks
carry the business name too. Filtering to bare-handle URLs gives 6 correct of 7
accepted at 30% recall. Meanwhile 6 of 12 Facebook Pages link their own Instagram
account, which is the same feeder for free. §6.7 has the numbers.

**Four defects were found by running it, and three failed silently.** Facebook
double-encodes its link shim, so every bio link truncated to the string `https` —
and the run honestly reported "0 websites filled", which is what a Page with no
bio link also looks like. The §7 breaker treated an ordinary empty Page as a
broken selector and blocked 29 Pages while **all 77 renders had returned HTTP
200**. A WhatsApp button number was never line-classified, landing as `confirmed`
with `line_type=unknown` — and §10.2 qualifies on "≥60 **and a mobile**", so the
most valuable row the stage produces was the one the export would drop. Each is
now pinned by a test. This is §5.5's failure mode one layer below where §5.5
expects it: not a source returning zero, but a *field* returning zero inside a
source that otherwise looks healthy.

**Screen 1's runtime estimate now includes Stage 3.** At 19 s per business
(measured over 127 renders) and 28% of businesses carrying a social URL, the
social pass is ~45 minutes on Lahore × food against ~12 for discovery. Enabling
the toggles without teaching `services/estimates.py` about them would have made
Screen 1 understate runtime by a factor of four — the precise dishonesty that
screen exists to avoid.

**Not built: Tier 4 (§6.5), the operator queue.** `operator_queue_cap` has been
in settings since Phase 1 and still has no reader.

### Phase 6 shipped a negative result, and that is the deliverable

The §5.3 directory layer is built, tested and correct, and across four live
slices it added **nothing**:

| Slice | Maps businesses | Directory listings | Matched | Contacts added |
|---|---|---|---|---|
| Lahore × food | 428 | 97 | 3 | **0** |
| Islamabad × salon | 199 | 35 | 2 | **0** |
| Lahore × salon | 60 | 109 | 1 | **0** |
| Karachi × salon | 39 | 92 | 1 | **0** |

The failure is a **join** failure, not a data failure. BusinessList holds real
Lahore restaurants this Maps run never surfaced — Cafe Zouk, Butt Karahi, Texas
Chicken — and none of them can be safely attached, because the directory
publishes *geocoded approximations* rather than surveyed positions. "Dilara's
Salon" and "Dilara Salon" are the same business **391 m** apart; one Lahore pair
is 13,196 km apart. §10.1's 150 m radius was calibrated Maps-against-Maps, where
both sides come from one survey. Widening it to 500 m was tested: it buys **zero
correct contacts and one incorrect one**. Inserting the unmatched rows as
discovery was tested too — 91 rows into Karachi × salon, **max score 45, zero
qualified**.

So `directories` now defaults **off**, and the run form says what was measured
instead of promising a source that will not deliver. The module is kept because
the finding is reproducible and the next person to read "directories are a
corroboration layer" needs the numbers that say what that is worth.

**The useful consequence is for Phase 7.** The constraint on a new source is
whether its records can be *joined* to the ones we already have. §5.4's PakPlay
embeds Maps' own `place_id` in its venue-page iframe, so it joins through
§10.1's tier 2 — an identity assertion, no name ratio, no distance test. That is
the property to select for.

Stages 1, 2, 5 and 6 have real bodies and run end to end. Stages 3 and 4 raise
`StageNotImplementedError` naming the phase that will build them — see
[stages.py](backend/src/leadscraper/pipeline/stages.py). The API reads
`IMPLEMENTED_STAGES` and refuses a run it cannot perform rather than starting one
that would silently omit a source the operator chose.

### What a run produces today

Two live runs, both `salon`, discovery followed by website enrichment:

| | Islamabad | Lahore |
|---|---|---|
| Businesses discovered | 199 | 60 |
| With a phone | 174 | 60 |
| Websites crawled | 62 domains | 31 domains |
| **Phone numbers after enrichment** | **256** | **110** |
| **WhatsApp `confirmed`** | **53** | **5** |
| Emails | 41 | 24 |
| Mean `lead_score` | 46.4 | 54.3 |
| **Qualified** (≥ 60 + a mobile) | **45** | **22** |

Before Phase 3 the `confirmed` column was structurally empty — §9.3 scores a
bare `03xx` from a Maps listing at 0.60, *likely*, and nothing else in the
pipeline could do better. Only a business's own site proves a number takes
WhatsApp. Measurements and the caveats on them are in §5.2.

Scores top out at **85, not 100**, until Phase 9: §10.2 gives 15 points to person
attribution and §8's engine does not exist yet, so the term is 0 on all but 1
business in 199. Each run reports this as `unattributed_ceiling` rather than
inflating the other weights to hide it — see §10.2.

```bash
# Stage 1 — discovery (needs a PK proxy, or an explicit opt-out; see §7.1)
PROXY_REQUIRED_SOURCES="" uv run python scripts/run_discovery.py \
    --city Lahore --category salon --synonyms 1 --tiles 3

# Stage 2 — website enrichment over that run
uv run python scripts/run_enrichment.py --latest

# Stage 2's other input — §5.3 directory corroboration. Measured at zero yield;
# see the Phase 6 note above before expecting anything from it
uv run python scripts/run_directories.py --latest

# Stage 3 — §6 social. Renders a browser per business at §6.6's 8-20s pacing,
# so budget ~20s each. Cached for 30 days: a second pass makes no requests
uv run python scripts/run_social.py --latest

# Stages 5 and 6 — score, rank, dedupe. Pure DB work: no network, no browser
uv run python scripts/run_scoring.py --latest
uv run python scripts/run_scoring.py --latest --preference whatsapp_only
```

## Running the app

Three processes. The scripts above still work and are the quickest way to drive a
single stage.

```bash
docker compose up -d                                   # Postgres :5433 + Redis :6379

cd backend
uv run uvicorn leadscraper.api.app:app --reload         # API      :8000
uv run python scripts/worker.py                         # the queue consumer

cd ../frontend
npm install && npm run dev                              # UI       :3000
```

**Start the worker, or nothing consumes the queues** — a created run sits at
`queued` for ever, which looks like a hang rather than a missing process. The
Settings screen says so out loud when it happens. `QUEUE_SYNC=true` runs stages
inline in the API instead, which is what the tests use.

RQ's default worker forks per job and `os.fork` does not exist on Windows, so
[worker.py](backend/scripts/worker.py) selects `SimpleWorker` there.

## Setup

Requires Docker, and Python 3.12+ via [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env          # edit if ports 5433/6379 are taken
docker compose up -d          # Postgres 16 + Redis 7
cd backend
uv sync
uv run alembic upgrade head
uv run pytest
```

Playwright is an optional extra so the Phase 1 install stays fast:

```bash
uv sync --extra browser
uv run playwright install chromium
```

## Layout

```
implementation.md            design doc — read this first
docker-compose.yml           Postgres + Redis
backend/
  config/
    cities.yaml              §3.1 cities + §5.1 commercial tiles
    synonyms.yaml            §4.2 query expansion — the highest-leverage config
  src/leadscraper/
    config.py                settings; the only reader of os.environ
    enums.py                 controlled vocabularies shared by DB, API, scrapers
    taxonomy.py              city/tile/synonym loading, §4 source routing
    core/
      phone.py               §9.1 extraction, §9.2 classification
      whatsapp.py            §9.3 evidence scoring
      cache.py               §7 content-addressed raw archive
      proxy.py               §7.1 egress routing
      pacing.py              §7 delays, per-source circuit breaker, budgets
      textnorm.py            §10.1 name normalisation, social-vs-website routing
      maps_payload.py        §5.1 positional search-payload parser
      webparse.py            §5.2 one page → wa.me, widgets, tel:, JSON-LD, email
      site_evidence.py       §5.2+§9.3 a domain's pages → scored findings
      geo.py                 §10.1 haversine + the blocking grid for dedupe
      scoring.py             §10.2 lead score, and what "missing" means to it
      ranking.py             §3.3 ranking by number_preference
    sources/
      google_maps.py         §5.1 grid fan-out, payload interception (Playwright)
      website.py             §5.2 cache-first crawler, 4 pages/domain (httpx)
      businesslist.py        §5.3 the one directory that survived recon (httpx)
      social.py              §6.4 FB/IG profiles, rendered logged out (Playwright)
    services/
      discovery.py           Stage 1 orchestration
      ingest.py              §10.1 cross-query dedupe and gap-filling merge
      enrichment.py          Stage 2 orchestration and the contact merge rules
      directories.py         Stage 2's second input — the §5.3 corroboration join
      social.py              Stage 3 — §6 Tier 3, and the bio-link routing
      scoring.py             Stage 5 — normalise, score, rank
      dedupe.py              Stage 6 — the §10.1 cascade and merge
      results.py             the read side — §13 filters, §15 suppression, §10.1 union
      estimates.py           §13 Screen 1's estimate, and what it refuses to say
    export/
      columns.py             §12.1's 41 columns, as data
      rows.py                one business + contacts → one row. Pure
      csv_writer.py          §12.2 — BOM, Excel armour, filename
    api/
      app.py                 the FastAPI app
      deps.py                the §13 filter bar, shared by table and export
      routes/                runs · results · suppression · meta
    db/models.py             §11 schema
    pipeline/
      queues.py              §2 per-stage queues, enqueue, cancel
      stages.py              the six stage entry points
      jobs.py                what a worker runs; status, timing, source pills
  scripts/
    run_discovery.py         drive Stage 1 directly
    run_enrichment.py        drive Stage 2's website pass over an existing run
    run_directories.py       drive Stage 2's §5.3 directory pass over a run
    run_social.py            drive Stage 3's §6 social pass over a run
    run_scoring.py           drive Stages 5–6 over an existing run
    spike_directories.py     §5.3 recon; --categories audits the slug mapping
    spike_social.py          §6 recon; --profiles/--render/--serper/--bio-links
    worker.py                the queue consumer
  tests/
frontend/                    §13's three screens + Settings (Next.js, TanStack)
  app/
    page.tsx                 Screen 1 — new run, and the honest estimate
    runs/[id]/page.tsx       Screen 2 — progress, source pills, cancel
    results/page.tsx         Screen 3 — the table, export, bulk delete
    settings/page.tsx        §13 Settings, read-only
  lib/api.ts                 the backend as types; one filter → one query string
```

## Things worth knowing before you change anything

**The cache is not an optimisation.** §7 calls it the single biggest lever, and
§2 depends on it for re-parsing: when a selector breaks you re-read stored bodies
instead of re-scraping. Never add a fetch path that bypasses it.

**Maps refuses to run without a PK proxy, on purpose.** Maps geo-ranks results,
so a US egress IP answers a Lahore query with the wrong businesses. Falling back
to direct would produce a full run of plausible, wrong data, so
`resolve_proxy("google_maps")` raises instead. This is correctness, not evasion.

**We never test whether a number has WhatsApp.** There is no legitimate API for
it and automating WhatsApp Web gets the probing number banned. `whatsapp.py`
scores published evidence and nothing in it should ever make a network call.

**§4.1 exclusions are encoded in `taxonomy.EXCLUDED_SOURCES`** with their
reasons, so LinkedIn/Apollo/Daraz cannot get quietly re-added by someone who
hasn't read why they were rejected.

**Unimplemented stages raise rather than returning empty.** A stage that
silently yields zero rows is the §5.5 failure mode — you harvest nothing and
don't notice.

**Evidence only ever moves up.** Stage 2 raises a contact's §9.3 score when the
business's site proves the number, and records *which page* proved it in
`wa_evidence_url`. It never lowers one: a site that happens not to mention
WhatsApp is not evidence against a number. The same rule covers person names and
confidence — this stage gap-fills and leaves Stage 4's territory alone.

**A field the source never published is not a zero.** This is the single
load-bearing rule in the scorer. §10.2 feeds `business_signal` from rating and
reviews, and `review_count` is present on 80% of the Islamabad run and **0%** of
the Lahore one — scoring that gap as "no reviews" would rank an entire run below
another for a payload artefact. Missing terms are dropped from the numerator
*and the denominator*. The mirror-image mistake is just as real: scoring rating
alone at full weight made Lahore come out 1.8 points too high, so a term with one
of its two inputs carries half its weight. §10.2 has the measurements.

**Dedupe merges on `place_id` alone; everything else needs the 150m test too.**
§10.1 lists an exact-phone tier and a domain tier as standalone merge keys. In
the live data, 36 groups of businesses share a number and 7 share a domain, and
**not one is a duplicate** — they are multi-branch chains (House of Salons ×3,
Royli, COSMO, Shelby's, Bella Care) and unrelated shops in one plaza. Applied
literally those tiers would delete 18 contactable premises across the two runs.
Phone and domain are demoted to corroboration that lowers the name bar. A merge is
also refused when both names declare a clientele and they differ — "Lavish Women
Salon" and "Lavish Men's Salon", 3m apart on one domain, score 93.1 and are two
separately-staffed premises.

**A discovery-only run has no qualified leads, by construction.** Three
unenriched Lahore × salon runs sit in the database next to the enriched one: same
city, same category, same discovery code, **0 qualified against 22**. Every
number from a Maps listing is a §9.3 *likely* at 0.60 and nothing else in the
record lifts a business over 60. Stage 2 is not an uplift on this scale — it is
the difference between a table and an empty filter.

**A distance test is only as good as the worse of its two coordinate sources.**
§10.1's 150 m radius was calibrated Maps-against-Maps, where both sides come from
one survey. BusinessList publishes *geocoded approximations* — "Dilara's Salon"
and "Dilara Salon" are the same business 391 m apart — so across sources the test
measures the two sources' disagreement about where a business is, not the
distance between two businesses. This is why §5.3's whole layer joins nothing,
and why a source that ships Maps' own `place_id` is worth more than a source that
ships better data.

**The table and the CSV are one query, not two that agree.** §12.2 says the export
must respect the active filters and sort order, which makes any divergence a
defect *by definition* — so there is one `ResultQuery`, one `fetch_results`, and
one FastAPI filter dependency behind both endpoints. Sorting is server-side for
the same reason: a browser-side sort would export a differently-ordered file from
the view the button was clicked on. Never give the exporter its own filter
parsing.

**Deleting is not removing.** §15 needs a removal to survive re-runs, and deleting
a business row does not — the next run rediscovers it from the same Maps listing.
So bulk delete writes the `do_not_contact` entries *first* and deletes in the same
transaction. The suppression is the durable artifact; the row deletion is
cosmetic. Deleting without suppressing is allowed and always returns a warning
saying the rows will come back.

**Screen 1 will not invent an availability number.** Per-query yield varies **3.4×
across cities inside one category** (Islamabad 66, Lahore 20, Karachi 19.5), and
the two Lahore runs disagree in the wrong direction — 3 queries gave 60 unique,
6 gave 52. So `services/estimates.py` reports *runtime* (ours, from the query plan
and §7 pacing) and refuses *availability* for any slice that has never been run.
§5.2 requires this explicitly. Do not add a multiplier here.

**A 200 is not a page.** Instagram answers a logged-out fetch with HTTP 200 and
605 KB of JavaScript containing nothing; Facebook answers with HTTP 400. Neither
is a wall and neither is an error you can retry — they are what a non-browser
client gets, and the answer is to render the page or to record `blocked`, never
to dress the client up as something it is not (§6.1). The corollary is that a
rendered body and a fetched body are **different artifacts** and must not share a
§7 cache key, or the module "hits cache" on a body that provably contains
nothing. `sources/social.py` keys renders separately for exactly this reason.

**For websites, a refusing host is not a refusing source.** §7's circuit breaker
is per source, which is right for Maps. The website module is a few hundred
unrelated hosts, and one 403 measurably cost a live run 19 healthy domains, so a
refusal there abandons that domain only. Ten consecutive refusals — which means
our egress is blocked, not one strict host — still stop the module. §5.2 records
the measurement.

## Departures from implementation.md

Seven, all deliberate, all covered by a test that explains itself:

1. **`businesses.place_id` is unique per run, not globally.** §11's `TEXT UNIQUE`
   would make a business scrapeable exactly once ever, so the second run of
   Lahore × salon could not insert anything the first run saw — while §16 asks
   you to re-run for validation.
2. **`contacts.belongs_to` added.** §12.1 exports `phone_N_belongs_to`; §11's SQL
   has nowhere to put it.
3. **`phone.py` does not use §9.1's literal regex.** Three of the eight formats
   §9.1 lists as verified in live data do not match the regex §9.1 publishes.
   `test_published_regex_is_insufficient_for_its_own_examples` pins exactly which.
4. **`contacts.wa_evidence_url` added.** §5.2's whole job is proving a number
   Maps published, so the URL the number came from and the URL that proved it
   are routinely different pages. §11 has one `source_url` and nowhere to record
   the second, which would silently drop the provenance §1 requires.
5. **§10.1's phone and domain tiers do not merge on their own.** They require the
   same 150m distance test tier 3 specifies. Measured on both runs, every group
   sharing a number or a domain is a chain or a coincidence and none is a
   duplicate; `test_dedupe.py` pins the real cases and the 54.5 name-similarity
   ceiling that sets the corroborated threshold.
6. **`directories` defaults off, where §3 lists it as "core, default on".** It
   was measured at 0 added contacts across 4 slices and 333 listings (§5.3). A
   source that is on by default and always contributes zero is §5.5's failure
   mode wearing a toggle.
7. **Three of §5.3's four directories are refused, including UrduPoint, which
   §16's Phase 6 row names by name.** Recorded in `taxonomy.EXCLUDED_SOURCES`
   with the recon that condemned each — UrduPoint carries 4% mobiles and 0%
   owner names at 171 KB per business, against §5.3's "clean field table, mostly
   `03xx`, has an owner-name field".

`do_not_contact` (§15) and `source_state` (§7) are also present; §15 requires the
former in v1 but §11's SQL omits it. Both got their first reader and first writer
in Phase 5 — `source_state` had been empty since Phase 1 because
`BreakerRegistry` is in-process and dies with the worker.

## What a run produces, end to end

```
Islamabad × salon  ·  199 businesses  ·  45 scoring ≥ 60  ·  28 with a confirmed number
                      → GET /api/runs/{id}/export.csv?min_score=60
                      → Islamabad_salon_20260810_45leads.csv   41 columns, 31 KB
```

The four Lahore × salon runs hold 232 `place_id` rows that are **72 real
businesses**; the results table collapses them at query time with
`?collapse=true` and modifies no run (§10.1).

## Next: the §16 validation pass — it needs a human

The exporter exists to produce its sample. Take 50 random rows and check: is the
phone correct and reachable, is `phone_1` genuinely the best number under the
chosen preference, do `confirmed` labels hold up when dialled, what is the true
duplicate rate. Two findings make it urgent — the ≥ 60 qualification bar has
**never been calibrated**, and §14's projected 63% qualification rate measured at
15–37% across four slices. Tune the weights against that ground truth *before*
building more source modules.

Phase 6 is weak evidence for doing that first: it was built ahead of the
validation and returned nothing. It also sharpened what to build next — the
binding constraint on a new source is **whether its records can be joined to the
ones we have**, and §5.4's PakPlay is the only remaining source that ships Maps'
own `place_id` as a join key.

Still open, and unchanged by Phase 6: no PK residential proxy, so all counts are
from a direct connection. §7.1's "returns the wrong businesses entirely" was
corrected by measurement — 429/429 Lahore addresses, 0 blocked queries — but
whether a PK IP surfaces a *different or larger* set has still never been A/B'd.
