"""The §13 HTTP surface.

The tests that matter most here are the ones about what the API *refuses*: a run
it cannot perform, an availability number it cannot justify, and a deletion that
would not survive the next run. §5.5's failure mode — "harvest 1,500 blank rows
and not notice" — reaches the API as a run that reports ``done`` having done
nothing, and these pin the paths that prevent it.
"""

from __future__ import annotations

import csv
import io
import itertools
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from leadscraper.api import deps
from leadscraper.api.app import app
from leadscraper.core.proxy import ProxyNotConfiguredError
from leadscraper.db.models import Business, Contact, DoNotContact, Run
from leadscraper.enums import (
    ContactKind,
    LineType,
    NumberPreference,
    RunStatus,
    Source,
    WhatsAppLabel,
)
from leadscraper.export import COLUMNS
from leadscraper.export.csv_writer import BOM
from tests.conftest import requires_db

_SEQUENCE = itertools.count(1)


@pytest.fixture
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client whose handlers use the rolled-back test session.

    Enqueueing is stubbed: these tests are about the HTTP contract, and a real
    ``enqueue`` would either need a worker or, in sync mode, run Playwright.
    """
    app.dependency_overrides[deps.db_session] = lambda: db_session
    monkeypatch.setattr(
        "leadscraper.api.routes.runs.enqueue_run", lambda run_id, stages: "job-1"
    )
    monkeypatch.setattr(
        "leadscraper.api.routes.runs.enqueue_stage",
        lambda run_id, stage, chain=True: "job-2",
    )
    monkeypatch.setattr("leadscraper.api.routes.runs.queue_depths", dict)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def no_proxy_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """§7.1 opt-out, as ``PROXY_REQUIRED_SOURCES=""`` does on the command line."""
    monkeypatch.setattr("leadscraper.api.routes.runs.resolve_proxy", lambda source: None)


def _number() -> str:
    return f"+9230077{next(_SEQUENCE):05d}"


def _seed(session: Session, *, city="Islamabad", score=72, **business_kw) -> Run:
    run = Run(
        city=city,
        category="salon",
        number_pref=NumberPreference.OWNER_FIRST,
        sources_enabled={"google_maps": True, "business_website": True},
        status=RunStatus.DONE,
        stats={"normalise_score": {"qualified": 1, "unattributed_ceiling": 85}},
    )
    session.add(run)
    session.flush()
    business = Business(
        run_id=run.id,
        name="Paragon Salon",
        name_norm="paragon salon",
        city=city,
        area="F-7",
        address="Jinnah Super",
        place_id=f"place-{uuid.uuid4()}",
        rating=4.6,
        review_count=31,
        lead_score=score,
        **business_kw,
    )
    session.add(business)
    session.flush()
    session.add(
        Contact(
            business_id=business.id,
            kind=ContactKind.PHONE,
            value_raw="0300 7700001",
            value_e164=_number(),
            line_type=LineType.MOBILE,
            wa_evidence=0.95,
            wa_label=WhatsAppLabel.CONFIRMED,
            confidence=0.9,
            source=Source.GOOGLE_MAPS,
            source_url="https://maps.google.com/?cid=1",
            rank=1,
        )
    )
    session.flush()
    return run


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #


@requires_db
def test_health_reports_whether_anything_will_consume_a_run(client: TestClient):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert "queue" in body


@requires_db
def test_pickers_read_from_the_taxonomy_not_a_second_list(client: TestClient):
    """§4.2's synonym dictionary is "the highest-leverage config"; a copy of it
    in TypeScript would be the fastest way to lose that."""
    cities = client.get("/api/meta/cities").json()
    assert any(c["name"] == "Lahore" and c["tile_count"] > 0 for c in cities)

    categories = client.get("/api/meta/categories").json()
    salon = next(c for c in categories if c["name"] == "salon")
    assert salon["synonym_count"] > 0
    # §4: only three of the seven categories have a real vertical directory.
    assert salon["vertical_strength"] == "none"


@requires_db
def test_stage_endpoint_names_the_phase_for_each_missing_stage(client: TestClient):
    body = client.get("/api/meta/stages").json()
    assert "discovery" in body["implemented"]
    missing = {m["stage"]: m["phase"] for m in body["missing"]}
    assert missing["social_enrichment"] == "Phase 8"
    assert missing["person_attribution"] == "Phase 9"


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


@requires_db
def test_run_with_facebook_is_refused_and_names_phase_8(
    client: TestClient, no_proxy_gate: None
):
    """§5.5: a run that cannot do the work must not be started and reported done.

    Stage 3 has no body, so enabling Facebook would produce a run that silently
    omits a source the operator explicitly chose.
    """
    response = client.post(
        "/api/runs",
        json={"city": "Lahore", "category": "salon", "sources": {"facebook": True}},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["stages"] == ["social_enrichment"]
    assert "Phase 8" in detail["message"]


@requires_db
def test_maps_run_is_refused_without_the_proxy_gate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """§7.1 — Maps geo-ranks, so a non-PK IP answers a Lahore query with the
    wrong businesses. Falling back to direct would produce a full run of
    plausible, wrong data, so the run is refused rather than started."""

    def _raise(source: str):
        raise ProxyNotConfiguredError("No proxy configured for google_maps.")

    monkeypatch.setattr("leadscraper.api.routes.runs.resolve_proxy", _raise)

    response = client.post("/api/runs", json={"city": "Lahore", "category": "salon"})
    assert response.status_code == 422
    assert "geo-ranks" in response.json()["detail"]["message"]


@requires_db
def test_unknown_city_is_refused_with_the_known_list(
    client: TestClient, no_proxy_gate: None
):
    response = client.post("/api/runs", json={"city": "Atlantis", "category": "salon"})
    assert response.status_code == 422


@requires_db
def test_directories_are_accepted_but_warned_about(
    client: TestClient, no_proxy_gate: None
):
    """Additive rather than missing, so the run is still the run that was asked
    for — but §5.5 says never let a source quietly contribute nothing."""
    response = client.post(
        "/api/runs",
        json={"city": "Lahore", "category": "salon", "sources": {"directories": True}},
    )
    assert response.status_code == 201
    assert any("Phase 6" in w for w in response.json()["warnings"])


@requires_db
def test_created_run_declares_which_stages_will_run(
    client: TestClient, no_proxy_gate: None
):
    body = client.post("/api/runs", json={"city": "Lahore", "category": "salon"}).json()
    assert body["stages_planned"] == [
        "discovery",
        "contact_enrichment",
        "normalise_score",
        "dedupe_export",
    ]


# --------------------------------------------------------------------------- #
# §13 Screen 1's estimate
# --------------------------------------------------------------------------- #


@requires_db
def test_estimate_gives_runtime_but_refuses_availability_for_an_unseen_slice(
    client: TestClient,
):
    """§5.2: "Measure per slice; do not extrapolate"."""
    body = client.post(
        "/api/meta/estimate", json={"city": "Faisalabad", "category": "entertainment"}
    ).json()

    assert body["runtime_minutes"]["low"] > 0
    assert body["available"] is None
    assert body["available_basis"] == "no_prior_run"
    assert body["caveats"]


# --------------------------------------------------------------------------- #
# §13 Screen 2
# --------------------------------------------------------------------------- #


@requires_db
def test_run_detail_carries_per_stage_counters_and_the_score_ceiling(
    client: TestClient, db_session: Session
):
    """§13 Screen 2's counters read ``runs.stats``, which every stage already
    writes — not a second counter path that could disagree with the summary."""
    run = _seed(db_session)
    body = client.get(f"/api/runs/{run.id}").json()

    assert {s["stage"] for s in body["stages"]} == {
        "discovery",
        "contact_enrichment",
        "social_enrichment",
        "person_attribution",
        "normalise_score",
        "dedupe_export",
    }
    # §10.2: the practical ceiling is 85 until Phase 9, and the UI must not imply
    # the missing 15 points are a fact about the business.
    assert body["unattributed_ceiling"] == 85


@requires_db
def test_cancelling_a_finished_run_is_a_conflict_not_a_no_op(
    client: TestClient, db_session: Session
):
    run = _seed(db_session)
    assert client.post(f"/api/runs/{run.id}/cancel").status_code == 409


# --------------------------------------------------------------------------- #
# §13 Screen 3 and §12.2
# --------------------------------------------------------------------------- #


@requires_db
def test_results_endpoint_returns_the_section_12_1_projection(
    client: TestClient, db_session: Session
):
    run = _seed(db_session)
    body = client.get("/api/results", params={"run": str(run.id)}).json()

    assert body["columns"] == list(COLUMNS)
    assert len(body["compact_columns"]) == 12
    assert body["total"] == 1
    assert body["rows"][0]["business_name"] == "Paragon Salon"


@requires_db
def test_export_is_utf8_with_bom_and_the_section_12_2_filename(
    client: TestClient, db_session: Session
):
    run = _seed(db_session)
    response = client.get(f"/api/runs/{run.id}/export.csv")

    assert response.status_code == 200
    assert response.text.startswith(BOM)
    disposition = response.headers["content-disposition"]
    assert "Islamabad_salon_" in disposition and "1leads.csv" in disposition


@requires_db
def test_exported_phone_is_armoured_against_excel(
    client: TestClient, db_session: Session
):
    run = _seed(db_session)
    text = client.get(f"/api/runs/{run.id}/export.csv").text
    rows = list(csv.reader(io.StringIO(text.removeprefix(BOM))))
    header, data = rows[0], rows[1]
    assert data[header.index("phone_1")].startswith('="+92')


@requires_db
def test_export_respects_the_same_filters_as_the_table(
    client: TestClient, db_session: Session
):
    """§12.2: "Respect the active table filters and sort order".

    One filter dependency feeds both endpoints, so this is the test that would
    fail if someone gave the exporter its own parsing.
    """
    run = _seed(db_session, score=72)
    params = {"run": str(run.id), "min_score": 90}

    table = client.get("/api/results", params=params).json()
    export = client.get(f"/api/runs/{run.id}/export.csv", params={"min_score": 90})
    body = list(csv.reader(io.StringIO(export.text.removeprefix(BOM))))

    assert table["total"] == 0
    assert len(body) == 1, "header only — the filtered-out row is absent from both"
    assert export.headers["X-Leads-Total"] == "0"


@requires_db
def test_export_of_a_thin_run_produces_a_header_only_file(
    client: TestClient, db_session: Session
):
    """The four discovery-only runs have 0 leads ≥ 60 by construction (§10.2).
    An empty export must still be a valid, openable CSV rather than an error."""
    run = _seed(db_session, score=45)
    response = client.get(f"/api/runs/{run.id}/export.csv", params={"min_score": 60})

    assert response.status_code == 200
    assert response.text.startswith(BOM)
    assert "0leads.csv" in response.headers["content-disposition"]


# --------------------------------------------------------------------------- #
# §15
# --------------------------------------------------------------------------- #


@requires_db
def test_bulk_delete_suppresses_before_deleting(client: TestClient, db_session: Session):
    """§15: a removal "goes in permanently, and it survives re-runs".

    Deleting the row alone does not achieve that — the next run of the same city
    and category rediscovers the same salon from the same Maps listing. The
    ``do_not_contact`` entry is the durable half.
    """
    run = _seed(db_session, website="https://paragon.pk")
    business = db_session.execute(
        Business.__table__.select().where(Business.run_id == run.id)
    ).first()
    number = db_session.query(Contact).filter_by(business_id=business.id).one().value_e164

    response = client.post(
        "/api/do-not-contact/bulk-delete",
        json={"business_ids": [str(business.id)], "reason": "asked to be removed"},
    )
    body = response.json()

    assert body["businesses_deleted"] == 1
    assert number in body["numbers_suppressed"]
    assert "paragon.pk" in body["domains_suppressed"]
    assert db_session.get(Business, business.id) is None
    # And the suppression outlives the row.
    assert db_session.query(DoNotContact).filter_by(value_e164=number).count() == 1


@requires_db
def test_deleting_without_suppressing_warns_that_it_will_not_stick(
    client: TestClient, db_session: Session
):
    run = _seed(db_session)
    business = db_session.execute(
        Business.__table__.select().where(Business.run_id == run.id)
    ).first()

    body = client.post(
        "/api/do-not-contact/bulk-delete",
        json={"business_ids": [str(business.id)], "suppress": False},
    ).json()

    assert body["suppressions_added"] == 0
    assert any("rediscovered" in w for w in body["warnings"])


@requires_db
def test_suppression_needs_something_to_match_on(client: TestClient):
    response = client.post("/api/do-not-contact", json={"reason": "no identifier"})
    assert response.status_code == 422


@requires_db
def test_a_suppressed_number_disappears_from_the_table_and_the_export(
    client: TestClient, db_session: Session
):
    """§15 says "checked at export time"; the table is checked too.

    A suppressed number still rendering as ``phone_1`` on screen is a number the
    operator rings, and §12.2 requires the file to match the screen anyway.
    """
    run = _seed(db_session)
    number = db_session.query(Contact).one().value_e164
    client.post("/api/do-not-contact", json={"value_e164": number})

    table = client.get("/api/results", params={"run": str(run.id)}).json()
    export = client.get(f"/api/runs/{run.id}/export.csv")

    assert table["total"] == 0
    assert table["suppressed_businesses"] == 1
    assert export.headers["X-Leads-Suppressed-Businesses"] == "1"
