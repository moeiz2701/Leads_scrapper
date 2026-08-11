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
    """Stage 2 — the business's own website (§5.2) and §5.3's directories.

    Two inputs, run in that order and reported separately in ``runs.stats``:

    * **§5.2, the business's own website** — the only source that can produce a
      `confirmed` WhatsApp label, via wa.me links, chat widgets, ``tel:`` and
      JSON-LD on a 4-page-per-domain budget and no browser.
    * **§5.3, BusinessList.pk** — a corroboration layer that joins directory rows
      to the businesses Maps already found. It runs *after* the website pass so
      that §5.2's evidence is already recorded when the directory's weaker,
      bare-number evidence arrives and the upgrade-only rule has something to
      refuse to overwrite.

    Each input is governed by its own ``sources_enabled`` flag, so a run may
    have either, both or neither. One input being disabled must never look like
    the other having failed, which is why the two write separate reports.

    Still outstanding here: the Maps detail-panel fallback for the ~13% of
    records whose search payload carried no phone (§5.1).
    """
    from leadscraper.db.models import Run
    from leadscraper.db.session import session_scope
    from leadscraper.services.directories import run_directory_corroboration
    from leadscraper.services.enrichment import run_website_enrichment

    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"No such run: {run_id}")
        sources = dict(run.sources_enabled or {})

    website_on = sources.get("business_website", True)
    directories_on = bool(sources.get("directories"))

    results: list[StageResult] = []
    if website_on:
        results.append(run_website_enrichment(run_id, limit=limit))
    if directories_on:
        results.append(run_directory_corroboration(run_id))

    if not results:
        # Neither input enabled. The caller asked for a stage with nothing in it,
        # and §5.5 says an empty success is the outcome to avoid above all — so
        # this raises rather than reporting a clean zero.
        raise ValueError(
            f"Stage {Stage.CONTACT_ENRICHMENT.value!r} was scheduled for run "
            f"{run_id} with neither 'business_website' nor 'directories' enabled. "
            f"There is no work for it to do."
        )

    return _combine(Stage.CONTACT_ENRICHMENT, run_id, results)


def _combine(stage: Stage, run_id: uuid.UUID, results: list[StageResult]) -> StageResult:
    """Fold a multi-input stage's results into the one §2 counter set.

    The per-input detail is *not* flattened away — each input already wrote its
    own report into ``runs.stats`` under its own key, and §13 Screen 2 reads
    those. This only sums what the queue and the CLI summary need.
    """
    combined = StageResult(stage=stage, run_id=run_id)
    for result in results:
        combined.processed += result.processed
        combined.produced += result.produced
        combined.skipped += result.skipped
        combined.failed += result.failed
        combined.notes.update(result.notes)
    return combined


def stage_social_enrichment(run_id: uuid.UUID, limit: int | None = None) -> StageResult:
    """Stage 3 — FB Page and IG profile, rendered logged out (§6.4, §6.7).

    Toggled off by default; §6.6 caps this at one request per business per run,
    and ``SocialSource`` enforces that rather than trusting the caller.

    Facebook is read before Instagram, which is **not** §6's stated ordering.
    §6.7 measured 58% of rendered FB Pages carrying an
    ``api.whatsapp.com/send?phone=`` button against 10% of IG profiles, and this
    stage exists to produce `confirmed` — so it follows the measurement. With the
    default cap of one request per business, a business holding both URLs is read
    on Facebook only.
    """
    from leadscraper.services.social import run_social_enrichment

    return run_social_enrichment(run_id, limit=limit)


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
        Stage.SOCIAL_ENRICHMENT,
        Stage.NORMALISE_SCORE,
        Stage.DEDUPE_EXPORT,
    }
)


def implemented_stages() -> list[Stage]:
    return [stage for stage in Stage if stage in IMPLEMENTED_STAGES]


def missing_stages() -> list[Stage]:
    return [stage for stage in Stage if stage not in IMPLEMENTED_STAGES]
