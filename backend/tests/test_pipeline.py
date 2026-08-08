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
    by calling the stage functions would mean running them."""
    assert implemented_stages() == [Stage.DISCOVERY]
    assert Stage.DISCOVERY not in missing_stages()
    assert set(implemented_stages()) | set(missing_stages()) == set(Stage)


def test_a_missing_run_fails_loudly(  ) -> None:
    """An implemented stage handed a nonexistent run must raise, not return a
    zero-count StageResult that reads like a successful empty run."""
    with pytest.raises(ValueError, match="No such run"):
        STAGE_FUNCTIONS[Stage.DISCOVERY](uuid.uuid4())
