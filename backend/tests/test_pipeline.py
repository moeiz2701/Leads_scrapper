"""§2 — pipeline wiring."""

from __future__ import annotations

import uuid

import pytest

from leadscraper.enums import Stage
from leadscraper.pipeline.queues import STAGE_TIMEOUTS
from leadscraper.pipeline.stages import (
    STAGE_FUNCTIONS,
    StageNotImplementedError,
    implemented_stages,
    missing_stages,
)
from tests.conftest import requires_db


def test_all_six_stages_are_registered() -> None:
    assert set(STAGE_FUNCTIONS) == set(Stage)
    assert len(Stage) == 6


def test_every_stage_has_a_timeout() -> None:
    assert set(STAGE_TIMEOUTS) == set(Stage)


@pytest.mark.parametrize("stage", missing_stages())
def test_unimplemented_stages_raise_rather_than_return_empty(stage: Stage) -> None:
    """A stage that silently produces zero rows is the §5.5 failure mode —
    you harvest nothing and do not notice. Until a stage has a body, it must
    fail loudly and name the phase that will build it."""
    with pytest.raises(StageNotImplementedError) as exc:
        STAGE_FUNCTIONS[stage](uuid.uuid4())
    assert "§16" in str(exc.value)
    assert exc.value.phase


def test_stage_registry_is_declared_not_probed() -> None:
    """The registry is a declaration that flips as each phase lands — probing it
    by calling the stage functions would mean running them.

    Phase 2 gave Stage 1 a body; Phase 3 gives Stage 2 the §5.2 website module.
    Stage 2's other inputs (the Maps detail-panel fallback, §5.3 directories)
    join that body later rather than replacing it, so the flag flips here once.
    Phase 4 adds Stages 5 and 6 — §10.2 scoring with §3.3 ranking, and the §10.1
    dedupe cascade. Stage 6's export half is Phase 5 and joins the same body.
    Phase 8 adds Stage 3, §6's social module — rendered rather than fetched, for
    the reasons §6.7 measured. Stage 4 (§8 attribution) is Phase 9 and is the
    last one still declaring itself missing.
    """
    assert implemented_stages() == [
        Stage.DISCOVERY,
        Stage.CONTACT_ENRICHMENT,
        Stage.SOCIAL_ENRICHMENT,
        Stage.NORMALISE_SCORE,
        Stage.DEDUPE_EXPORT,
    ]
    assert not set(implemented_stages()) & set(missing_stages())
    assert set(implemented_stages()) | set(missing_stages()) == set(Stage)


@requires_db
@pytest.mark.parametrize("stage", implemented_stages())
def test_a_missing_run_fails_loudly(stage: Stage) -> None:
    """An implemented stage handed a nonexistent run must raise, not return a
    zero-count StageResult that reads like a successful empty run.

    Marked ``requires_db`` because an implemented stage opens a session before
    it can discover the run is missing. Without the marker this fails with a
    connection error when Postgres is down, which reads like a broken stage and
    is not one.
    """
    with pytest.raises(ValueError, match="No such run"):
        STAGE_FUNCTIONS[stage](uuid.uuid4())
