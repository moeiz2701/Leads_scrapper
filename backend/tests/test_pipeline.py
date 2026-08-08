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


def test_all_six_stages_are_registered() -> None:
    assert set(STAGE_FUNCTIONS) == set(Stage)
    assert len(Stage) == 6


def test_every_stage_has_a_timeout() -> None:
    assert set(STAGE_TIMEOUTS) == set(Stage)


@pytest.mark.parametrize("stage", list(Stage))
def test_unimplemented_stages_raise_rather_than_return_empty(stage: Stage) -> None:
    """A stage that silently produces zero rows is the §5.5 failure mode —
    you harvest nothing and do not notice. Until a stage has a body, it must
    fail loudly and name the phase that will build it."""
    with pytest.raises(StageNotImplementedError) as exc:
        STAGE_FUNCTIONS[stage](uuid.uuid4())
    assert "§16" in str(exc.value)
    assert exc.value.phase


def test_stage_registry_is_declared_not_probed() -> None:
    """Phase 1 ships no stage bodies. This flips as each phase lands, and it
    must be a declaration — probing by calling the function means running it."""
    assert implemented_stages() == []
    assert missing_stages() == list(Stage)
