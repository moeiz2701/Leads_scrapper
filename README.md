# Pakistan Local Business Lead Scraper

Discovers local businesses by city + category and extracts contactable numbers,
prioritising WhatsApp-capable mobiles attributed to a named owner where the
public record supports it.

**The design document is [implementation.md](implementation.md).** It is the
source of truth — the code references it by section (§5.1, §9.2, …), and any
disagreement between the two should be resolved by fixing one of them, not by
ignoring it.

## Status

Phase 1 of the §16 build order is complete. Phases 2–10 are outstanding.

| | |
|---|---|
| ✅ Done | Schema + migrations, queue skeleton, raw-fetch cache, phone normaliser + PK classifier, WhatsApp evidence scorer, proxy abstraction, taxonomy/synonym config |
| ⬜ Next | Maps recon spike, then Phase 2 (Google Maps module) |

No stage has a body yet, so there is nothing to run end to end. Each stage
raises `StageNotImplementedError` naming the phase that will build it — see
[stages.py](backend/src/leadscraper/pipeline/stages.py).

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
    db/models.py             §11 schema
    pipeline/                §2 six-stage queue wiring
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

## Departures from implementation.md

Three, all deliberate, all covered by a test that explains itself:

1. **`businesses.place_id` is unique per run, not globally.** §11's `TEXT UNIQUE`
   would make a business scrapeable exactly once ever, so the second run of
   Lahore × salon could not insert anything the first run saw — while §16 asks
   you to re-run for validation.
2. **`contacts.belongs_to` added.** §12.1 exports `phone_N_belongs_to`; §11's SQL
   has nowhere to put it.
3. **`phone.py` does not use §9.1's literal regex.** Three of the eight formats
   §9.1 lists as verified in live data do not match the regex §9.1 publishes.
   `test_published_regex_is_insufficient_for_its_own_examples` pins exactly which.

`do_not_contact` (§15) and `source_state` (§7) are also present; §15 requires the
former in v1 but §11's SQL omits it.
