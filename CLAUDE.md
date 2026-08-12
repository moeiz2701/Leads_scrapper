# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Pakistan local-business lead scraper. Discovers businesses by city + category and
extracts contactable numbers, prioritising WhatsApp-capable mobiles attributed to a
named owner where the public record supports it.

## Two specs, and they cover different things

[implementation.md](implementation.md) is the pipeline: how a business is found,
enriched, scored and exported. [_BATCH_SPEC.md](_BATCH_SPEC.md) is the outreach
layer on top of it: which of seven mutually exclusive **batches** a lead falls
into, and therefore which message it gets. It is the results table's major
filter. Its §0 records what was built, the three departures from its prose, and
its scope — **the cascade is calibrated on `food` and routes nothing else**;
every other category resolves to `unbatched`, which is not a batch and is never a
send target. Widening that is a measurement (percentiles for the new vertical),
never an edit to a category list.

## The design document is the source of truth

**[implementation.md](implementation.md) is the spec, and the code references it by
section (§5.1, §9.3, §10.2, …).** Read it before changing anything non-trivial. If code
and doc disagree, fix one of them — do not silently ignore the difference.

Sections carry **dated correction notes added by measurement during the build**. Trust
those over the original prose: several of the doc's original claims were measured and
found wrong (§5.3's directory yield, §6.4's "no browser", §7.1's "wrong businesses
entirely"). A section with no correction note has never been measured, and its numbers
are assertions rather than findings — §6 was in that state until Phase 8, and three of
its four factual claims turned out to be wrong.

[README.md](README.md) carries current status, the seven documented departures from the
doc (each pinned by a test), and the "things worth knowing before you change anything"
list. Both files are kept current deliberately; update them in the same commit as the
code they describe.

## Commands

**`cd backend` before any `uv` command.** A bare `uv run` from the repo root picks up the
system Anaconda Python and fails confusingly.

```bash
# From the repo ROOT. Brings up everything: postgres :5433, redis :6379, the API
# :8000, the queue worker, and the frontend :3000 — plus a one-shot `migrate`
# service that api and worker wait on. `up -d postgres redis` for datastores only,
# which is what the shell workflow below wants.
docker compose up -d

cd backend
uv sync                       # see the Playwright warning below
uv run alembic upgrade head   # migrations; head is 4e2b8c05af31
uv run pytest                 # full suite
uv run pytest tests/test_social.py::test_a_429_stops_the_module_for_the_run  # one test
uv run ruff check .           # lint — must stay clean
uv run ruff format .
```

The green bar is **tests passing + ruff clean**. `mypy` is configured but reports errors
(all the same benign SQLAlchemy `float`→`Numeric` pattern plus one Playwright proxy-type
mismatch); whether to adopt it properly or drop it is undecided.

### Running the app — three processes

```bash
cd backend && uv run uvicorn leadscraper.api.app:app --reload    # API  :8000
cd backend && uv run python scripts/worker.py                    # queue consumer
cd frontend && npm run dev                                       # UI   :3000
cd frontend && npx tsc --noEmit                                  # typecheck
```

**Start the worker or nothing consumes the queues** — a created run sits at `queued`
for ever, which looks like a hang rather than a missing process. `QUEUE_SYNC=true` runs
stages inline in the API instead, which is what the tests use.

### Driving stages directly

Usually faster than the UI for development. Each takes `--run-id <uuid>` or `--latest`.

```bash
PROXY_REQUIRED_SOURCES="" uv run python scripts/run_discovery.py \
    --city Lahore --category food --synonyms 2 --tiles 3
uv run python scripts/run_enrichment.py --latest    # Stage 2, §5.2 websites
uv run python scripts/run_directories.py --latest   # Stage 2, §5.3 directories
uv run python scripts/run_social.py --latest        # Stage 3, §6 social
uv run python scripts/run_scoring.py --latest [--preference whatsapp_only]
```

```bash
uv run python scripts/spike_batches.py   # read-only: the batch split of every run
```

`run_scoring.py` is pure DB work, needs no network, and is safe to re-run.

## Architecture

Six stages (§2), chained through per-stage RQ queues. `pipeline/stages.py` holds the
entry points and `IMPLEMENTED_STAGES`; `pipeline/jobs.py` is what a worker runs.

| Stage | Source | State |
|---|---|---|
| 1 discovery | §5.1 Google Maps (Playwright, payload interception) | ✅ |
| 2 contact_enrichment | §5.2 business websites (httpx) + §5.3 directories | ✅ |
| 3 social_enrichment | §6 FB Pages + IG profiles, **rendered** logged out | ✅ |
| 4 person_attribution | §8 | raises `StageNotImplementedError("Phase 9")` |
| 5 normalise_score | §10.2 scoring, §3.3 ranking | ✅ |
| 6 dedupe_export | §10.1 cascade | ✅ |

The layering is consistent and worth matching when adding a source:

- `core/` — pure functions, no network, no DB. `phone.py` (§9.1/9.2), `whatsapp.py`
  (§9.3 evidence scoring), `webparse.py` (one HTML page → wa.me, widgets, `tel:`,
  JSON-LD, socials), `site_evidence.py` (a domain's pages → scored findings), `cache.py`
  (§7 content-addressed archive), `pacing.py`, `geo.py`, `scoring.py`,
  `textnorm.py`, `batches.py` (_BATCH_SPEC's cascade — food only).
- `sources/` — one module per external source. Cache-first fetch, own circuit breaker,
  own pacing policy. Parsing is a pure function taking `(url, body)` so it is testable
  from a string literal.
- `services/` — stage orchestration and the DB merge rules. `enrichment.py` is the
  reference implementation; `social.py` and `directories.py` follow it.
- `export/` — §12.1's 41 columns as data, one business+contacts → one row (pure).
- `api/` — FastAPI. `deps.py` holds the §13 filter bar shared by table, export
  **and extraction** — including the `batch` filter, which is validated against
  the catalogue and 422s on an unknown token rather than failing open like the
  others (failing open would *widen* the view and get a whole run extracted
  under one message).

**`config.py` is the only reader of `os.environ`.** Everything tunable resolves through
`Settings`.

## Rules that are load-bearing

These are not style preferences — each one exists because violating it produced wrong
data, and most are pinned by a test.

- **Never let a source silently return zero.** §5.5's failure mode has bitten this
  project repeatedly. Unimplemented stages raise rather than returning empty; a run that
  could not do its work reports `partial`, not `done` — different facts, not degrees.
- **Missing data stays missing, never 0 or a guess.** A business with no `review_count`
  exports blank. §10.2 drops missing terms from the numerator *and* the denominator; a
  term with one of its two inputs carries half its weight.
- **Evidence only ever moves up.** A new source raises a contact's §9.3 score and records
  which URL proved it (`wa_evidence_url`). It never lowers one — a page that happens not
  to mention WhatsApp is not evidence *against* a number.
- **Never discard a contact (§10.1).** The exporter caps at 4 phone slots by rank; it
  must not delete rows to do so.
- **Provenance survives everything (§1).** `source`/`source_url` say where a value came
  from; `wa_evidence_url` says where the proof came from. They are routinely different
  pages. §15's deletion path depends on both staying accurate.
- **Never make a network call to test WhatsApp.** There is no legitimate API and
  automating WhatsApp Web gets the probing number banned. `whatsapp.py` scores published
  evidence and nothing in it may reach the network.
- **Export the label, not the raw score (§9.3)** — `confirmed`/`likely`/`no`.
- **The table, the CSV and the clipboard are one query.** §12.2 makes divergence a defect
  by definition: one `ResultQuery`, one `fetch_results`, one filter dependency. If you add
  a filter, add it to `services/results.py`, never to an endpoint. Extraction reads the
  same query for the same reason and takes its filters from the *query string*, not from
  its POST body — a body that re-declared them would be a second place to drift.
- **A batch is assigned once, after §15, and recorded when it is sent.** The
  cascade runs on the *visible* contact set, so a business whose only WhatsApp
  number was suppressed is in `no-whatsapp` rather than in a send batch whose
  clipboard comes up short. `extractions.batch` stores what it was at pull time:
  a business that later gains a website moves batch, and the record of which
  message already went out must not move with it. NULL there means "not
  recorded" (pre-migration); `"unbatched"` means "no definition covers this
  category" — different facts, stored differently.
- **Extraction marks, it does not suppress.** `do_not_contact` (§15) says "never contact
  this"; `extractions` says "already sent". Clearing an entry puts the business back in
  the queue. Neither table is read or written from the other's code, and a pull writes no
  suppression. The mark decorates a row and must never become a filter — hiding extracted
  rows would shrink the CSV with them.
- **Deleting is not removing (§15).** Bulk delete writes `do_not_contact` entries *first*
  and deletes in the same transaction, because the next run would otherwise rediscover
  the row.
- **Never add a fetch path that bypasses `core/cache.py`.** §7 calls it the single
  biggest lever, and §2 depends on it for re-parsing: when a selector breaks you re-read
  stored bodies instead of re-scraping. Store bodies under both the requested and the
  final URL (§5.2's redirect trap).
- **Scores top out at 85 until Phase 9.** §10.2 gives 15 points to person attribution and
  §8's engine does not exist. Each run reports `unattributed_ceiling` rather than
  inflating the other weights to hide it.
- **Person attribution is Phase 9.** Gap-fill a name if one falls out, but never fabricate
  the name↔number join and do not build an attribution engine inside a source module.
- **§6.1/§5.5 scope boundary.** No anti-detection, no fingerprint spoofing, no CAPTCHA
  solving, no automating past a login wall. Automate interactions with public data; never
  automate past an access control. Rendering a public page in an ordinary logged-out
  browser is fine and is what Stage 3 does.

## Traps that have already cost time

- **`uv sync` PRUNES optional extras and will silently uninstall Playwright.** Use
  `uv sync --extra browser` if you touch dependencies, then `uv run playwright install
  chromium`.
- **The §7 cache will hide your network changes.** Listing TTL 7 days, detail 30. Check
  `pages_from_cache` / `from_cache` in the run stats before concluding anything about
  live behaviour.
- **Set `PYTHONIOENCODING=utf-8`** before running scripts that print business names, or
  cp1252 raises `UnicodeEncodeError` on Urdu and Arabic text.
- **Run only one pytest process at a time**, and never point tests at the main DB. Tests
  use a separate `leads_test` database, reset automatically by conftest.
- **Never run two scraping scripts concurrently.** Two processes writing the same cache
  key produce a duplicate-key crash, and the DB session only commits at stage end.
- **Postgres is on port 5433**, not 5432. Containers stop when Docker Desktop stops but
  the volumes survive; `docker compose up -d` brings the seven existing runs back.
- **A 200 is not a page.** Instagram answers a logged-out fetch with HTTP 200 and 605 KB
  of JavaScript containing nothing; Facebook answers with HTTP 400. A rendered body and a
  fetched body are different artifacts and must not share a cache key.
- **Windows has no `os.fork`**, so `scripts/worker.py` selects RQ's `SimpleWorker` there.
  In the Docker image it forks, because the container is Linux.
- **`API_URL` is a *build*-time value for the frontend image.** `next.config.ts` looks
  like it resolves the proxy target per request, but `rewrites()` runs during
  `next build` and its destination is frozen into `.next/routes-manifest.json`, which
  is what `next start` reads. Setting it only in compose's `environment:` produced a
  container that started cleanly and proxied everything to the `127.0.0.1:8000`
  fallback — ECONNREFUSED against an API that was up the whole time. It is passed as a
  `build.args` entry; change it in both places or in neither.
- **Maps refuses to run without a PK proxy on purpose** — `resolve_proxy("google_maps")`
  raises. This is correctness (results are geo-ranked), not evasion. Opt out explicitly
  with `PROXY_REQUIRED_SOURCES=""`.

## Testing conventions

Tests are documentation of *why*, not just *what*. Each non-obvious assertion carries a
docstring naming the section it pins and, where relevant, the live measurement that
forced it. When a phase changes behaviour a previous test pinned, **rewrite that test
rather than deleting it** — a deleted test leaves the rule unpinned in whichever
direction it now points (see `test_run_with_facebook_is_accepted_now_that_phase_8_landed`).

Parsing is tested from HTML string literals taken from real pages, plus one gzipped
fixture of the real body per source in `tests/fixtures/` to catch upstream markup
reshuffles. Sources take an injectable client/renderer so stage logic is testable without
network or browser.

## Working style for this project

Recon before code. §5.3 and §6 were both specced from unverified prose and both were
substantially wrong; one afternoon of fetching corrected each. Write a
`scripts/spike_*.py`, cache every body through `core/cache.py` so the measurement is
re-runnable at zero cost, report the numbers, then write them into implementation.md as a
correction note before building.

**A negative result honestly measured and documented is a valid phase outcome.** Phase 6
built §5.3's directory layer, measured it at 0 added contacts across four slices, and
shipped it defaulted off with the numbers recorded. That is the deliverable, not a
failure.

If you mutate the existing runs to measure something, restore them and verify against
their prior counts.
