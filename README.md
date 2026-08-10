# Pakistan Local Business Lead Scraper

Discovers local businesses by city + category and extracts contactable numbers,
prioritising WhatsApp-capable mobiles attributed to a named owner where the
public record supports it.

**The design document is [implementation.md](implementation.md).** It is the
source of truth — the code references it by section (§5.1, §9.2, …), and any
disagreement between the two should be resolved by fixing one of them, not by
ignoring it.

## Status

Phases 1–4 of the §16 build order are complete. Phases 5–10 are outstanding.

| | |
|---|---|
| ✅ Phase 1 | Schema + migrations, queue skeleton, raw-fetch cache, phone normaliser + PK classifier, WhatsApp evidence scorer, proxy abstraction, taxonomy/synonym config |
| ✅ Phase 2 | Google Maps discovery — grid × synonym fan-out, payload interception, scroll to exhaustion, pacing, circuit breaker, cross-query dedupe |
| ✅ Phase 3 | Business website module — wa.me, chat widgets, `tel:`, JSON-LD, emails, socials; 4-page crawl budget, no browser |
| ✅ Phase 4 | §10.2 lead scoring, §3.3 ranking by `number_preference`, §10.1 dedupe cascade |
| ⬜ Next | Phase 5 (frontend + CSV export — **ship here**) |

Stages 1, 2, 5 and 6 have real bodies and run end to end. Stages 3 and 4 raise
`StageNotImplementedError` naming the phase that will build them — see
[stages.py](backend/src/leadscraper/pipeline/stages.py). Stage 6's export half is
Phase 5 and joins the same body.

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

# Stages 5 and 6 — score, rank, dedupe. Pure DB work: no network, no browser
uv run python scripts/run_scoring.py --latest
uv run python scripts/run_scoring.py --latest --preference whatsapp_only
```

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
    services/
      discovery.py           Stage 1 orchestration
      ingest.py              §10.1 cross-query dedupe and gap-filling merge
      enrichment.py          Stage 2 orchestration and the contact merge rules
      scoring.py             Stage 5 — normalise, score, rank
      dedupe.py              Stage 6 — the §10.1 cascade and merge
    db/models.py             §11 schema
    pipeline/                §2 six-stage queue wiring
  scripts/
    run_discovery.py         drive Stage 1 before the Phase 5 API
    run_enrichment.py        drive Stage 2 over an existing run
    run_scoring.py           drive Stages 5–6 over an existing run
  tests/
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

**For websites, a refusing host is not a refusing source.** §7's circuit breaker
is per source, which is right for Maps. The website module is a few hundred
unrelated hosts, and one 403 measurably cost a live run 19 healthy domains, so a
refusal there abandons that domain only. Ten consecutive refusals — which means
our egress is blocked, not one strict host — still stop the module. §5.2 records
the measurement.

## Departures from implementation.md

Five, all deliberate, all covered by a test that explains itself:

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

`do_not_contact` (§15) and `source_state` (§7) are also present; §15 requires the
former in v1 but §11's SQL omits it.
