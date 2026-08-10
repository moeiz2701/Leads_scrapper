"""The six stage consumers (§2).

Phase 1 wires the plumbing; the stage bodies land in later phases per §16. Each
unimplemented stage raises rather than returning an empty result — a stage that
silently yields zero rows is exactly the §5.5 failure mode where you "harvest
1,500 blank rows and not notice", and it is worse on a stage boundary than on a
selector.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field

from leadscraper.enums import Stage
from leadscraper.logging import get_logger

log = get_logger(__name__)


class StageNotImplementedError(NotImplementedError):
    """Raised by a stage whose build phase has not landed yet."""

    def __init__(self, stage: Stage, phase: str) -> None:
        super().__init__(
            f"Stage {stage.value!r} is not implemented yet — scheduled for {phase} "
            f"in implementation.md §16."
        )
        self.stage = stage
        self.phase = phase


@dataclass(slots=True)
class StageResult:
    """What every stage hands back. Counters feed ``runs.stats`` and the UI."""

    stage: Stage
    run_id: uuid.UUID
    processed: int = 0
    produced: int = 0
    skipped: int = 0
    failed: int = 0
    notes: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["stage"] = self.stage.value
        data["run_id"] = str(self.run_id)
        return data


def stage_discovery(run_id: uuid.UUID, synonym_limit: int | None = None) -> StageResult:
    """Stage 1 — Maps grid fan-out (§5.1).

    Emits businesses *and* their listed phone numbers: per the §5.1 recon, the
    Maps search payload already carries the phone, so this stage does the work
    §14 originally costed as 700 separate detail-panel interactions. Directory
    and vertical discovery join here in Phases 6–7.
    """
    from leadscraper.services.discovery import run_maps_discovery

    return run_maps_discovery(run_id, synonym_limit=synonym_limit)


def stage_contact_enrichment(run_id: uuid.UUID, limit: int | None = None) -> StageResult:
    """Stage 2 — the business's own website (§5.2).

    The stage that produces `confirmed` WhatsApp labels: wa.me links, chat
    widgets, ``tel:`` and JSON-LD, on a 4-page-per-domain budget and no browser.

    Two of this stage's inputs are still outstanding and will join here rather
    than replacing what is below — the Maps detail-panel fallback for the ~13%
    of records whose search payload carried no phone (§5.1), and the directory
    corroboration layer (§5.3, Phase 6).
    """
    from leadscraper.services.enrichment import run_website_enrichment

    return run_website_enrichment(run_id, limit=limit)


def stage_social_enrichment(run_id: uuid.UUID) -> StageResult:
    """Stage 3 — FB Page public data, IG bio → bio-link follow (§6). Toggled off
    by default; §6.6 caps this at one request per business per run."""
    raise StageNotImplementedError(Stage.SOCIAL_ENRICHMENT, "Phase 8")


def stage_person_attribution(run_id: uuid.UUID) -> StageResult:
    """Stage 4 — name + role, linked to a phone, with an honest §8 tier."""
    raise StageNotImplementedError(Stage.PERSON_ATTRIBUTION, "Phase 9")


def stage_normalise_score(run_id: uuid.UUID) -> StageResult:
    """Stage 5 — E.164, WhatsApp evidence, lead score (§10.2), rank (§3.3).

    Scores are renormalised over the terms whose inputs the source published, so
    a business whose Maps payload omitted ``review_count`` is scored on what is
    known rather than docked for what is missing (§5.1).

    §8's attribution engine is Phase 9, so the 15-point person term is 0 for
    almost every row today and the practical ceiling is 85. The stage reports
    that as ``score_ceiling`` rather than inflating the other weights to hide it.
    """
    from leadscraper.services.scoring import run_normalise_score

    return run_normalise_score(run_id)


def stage_dedupe_export(run_id: uuid.UUID) -> StageResult:
    """Stage 6 — §10.1 dedupe cascade and merge. Export is Phase 5.

    Scoped to one run, and every tier except ``place_id`` must also pass §10.1's
    150 m distance test — applied literally, the phone and domain tiers merge
    each multi-branch chain into a single row. See ``services/dedupe``.
    """
    from leadscraper.services.dedupe import run_dedupe

    return run_dedupe(run_id)


STAGE_FUNCTIONS = {
    Stage.DISCOVERY: stage_discovery,
    Stage.CONTACT_ENRICHMENT: stage_contact_enrichment,
    Stage.SOCIAL_ENRICHMENT: stage_social_enrichment,
    Stage.PERSON_ATTRIBUTION: stage_person_attribution,
    Stage.NORMALISE_SCORE: stage_normalise_score,
    Stage.DEDUPE_EXPORT: stage_dedupe_export,
}


# Stages that have a real body. Each build phase adds its stage here as it lands;
# the API reads it to refuse a run with a clear message rather than enqueuing
# work that can only fail. Deliberately a declared list, not something probed by
# calling the functions — probing a stage means running it.
IMPLEMENTED_STAGES: frozenset[Stage] = frozenset(
    {
        Stage.DISCOVERY,
        Stage.CONTACT_ENRICHMENT,
        Stage.NORMALISE_SCORE,
        Stage.DEDUPE_EXPORT,
    }
)


def implemented_stages() -> list[Stage]:
    return [stage for stage in Stage if stage in IMPLEMENTED_STAGES]


def missing_stages() -> list[Stage]:
    return [stage for stage in Stage if stage not in IMPLEMENTED_STAGES]
