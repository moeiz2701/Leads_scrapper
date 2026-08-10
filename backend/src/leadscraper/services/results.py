"""The read side: §13 Screen 3's table and §12.2's CSV, from one query.

**This module exists so the table and the export cannot disagree.** §12.2 says
the CSV must "respect the active table filters and sort order", which makes any
divergence between the two a defect by definition — so there is one filter type,
one sort, one suppression check, and both endpoints call ``fetch_results``.

Three things happen here that are not in the §11 schema:

* **§15 suppression.** ``do_not_contact`` is consulted on every read, not just on
  export. §15 says "checked at export time", and that is the *minimum*: a
  suppressed number that still renders as ``phone_1`` on screen is a number the
  operator rings. Applied in one place, it applies to both.
* **§10.1's read-side union.** The §10.1 note defers "the operator wants one
  table, not four" to Phase 5 as a *query-time* collapse on ``place_id``, because
  a cross-run merge has nowhere to put the survivor. ``collapse_place_id`` is
  that, and it destroys nothing.
* **The §13 contact-level filters.** WhatsApp status, phone type and source are
  properties of a *contact*, but a row is a *business*, so each is read as "has a
  visible, ranked number matching this".

Filtering below the run level happens in Python rather than SQL. That is a
deliberate trade: the contact-level predicates are defined over the *exported*
contact set — which is ``rank``, suppression and §12.1's 4-slot cap together —
and expressing that in SQL means reimplementing §12.1 in a second language where
it can rot independently. A run is a few hundred businesses (199 is the largest
in the database); the run scoping and score floor are pushed into SQL so what is
materialised stays bounded by one run. Revisit if a run ever reaches five
figures.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from leadscraper.core.textnorm import registrable_domain
from leadscraper.db.models import Business, Contact, DoNotContact, Run
from leadscraper.enums import ContactKind
from leadscraper.export.rows import build_row

# Columns the table may be sorted by. An allow-list, because the sort key
# arrives from a query string.
SORTABLE: frozenset[str] = frozenset(
    {
        "lead_score",
        "business_name",
        "city",
        "area",
        "phone_count",
        "rating",
        "review_count",
        "scraped_at",
    }
)

DEFAULT_SORT = "lead_score"  # §13 Screen 3: "default `lead_score DESC`"


@dataclass(frozen=True, slots=True)
class ResultQuery:
    """§13 Screen 3's filter bar, as a value object."""

    run_ids: tuple[uuid.UUID, ...] = ()
    whatsapp: tuple[str, ...] = ()
    has_owner_name: bool | None = None
    min_score: int | None = None
    line_types: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    search: str | None = None
    sort: str = DEFAULT_SORT
    descending: bool = True
    collapse_place_id: bool = False
    limit: int | None = None
    offset: int = 0


@dataclass(slots=True)
class ResultPage:
    """Rows plus the counts the UI needs to be honest about what it hid."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    # §15 — reported rather than silently applied. An operator looking at 44 rows
    # where a colleague saw 45 needs to know a suppression did that.
    suppressed_contacts: int = 0
    suppressed_businesses: int = 0
    # §10.1 read-side union: how many rows the place_id collapse folded away.
    collapsed: int = 0
    cities: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Suppression:
    """§15's ``do_not_contact``, loaded once per query."""

    numbers: frozenset[str] = frozenset()
    emails: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.numbers or self.emails or self.domains)

    def blocks_contact(self, contact: Contact) -> bool:
        if contact.value_e164 and contact.value_e164 in self.numbers:
            return True
        if contact.kind == ContactKind.EMAIL:
            return contact.value_raw.strip().lower() in self.emails
        return False

    def blocks_business(self, business: Business) -> bool:
        """A whole business is out when its *domain* was suppressed.

        A single suppressed number removes that number. A suppressed domain is a
        different statement — the business itself asked not to be contacted — so
        the row goes, however many other numbers it has.
        """
        if not business.website:
            return False
        domain = registrable_domain(business.website)
        return bool(domain and domain in self.domains)


def load_suppression(session: Session) -> Suppression:
    rows = session.execute(select(DoNotContact)).scalars().all()
    return Suppression(
        numbers=frozenset(r.value_e164 for r in rows if r.value_e164),
        emails=frozenset(r.email.strip().lower() for r in rows if r.email),
        domains=frozenset(r.domain.strip().lower() for r in rows if r.domain),
    )


def fetch_results(session: Session, query: ResultQuery) -> ResultPage:
    """The one read path. Both §13 Screen 3 and §12.2 call this."""
    page = ResultPage()
    suppression = load_suppression(session)

    businesses = _select_businesses(session, query)
    visible: list[tuple[Business, list[Contact]]] = []

    for business in businesses:
        if suppression and suppression.blocks_business(business):
            page.suppressed_businesses += 1
            continue

        contacts = list(business.contacts)
        if suppression:
            kept = [c for c in contacts if not suppression.blocks_contact(c)]
            removed = len(contacts) - len(kept)
            if removed:
                page.suppressed_contacts += removed
                phones = [c for c in contacts if c.kind == ContactKind.PHONE]
                # Every number it had is suppressed: there is nothing left to
                # ring, so the row is not a lead. A business that never had a
                # phone is a different case and stays — it was never suppressed.
                if phones and not any(c.kind == ContactKind.PHONE for c in kept):
                    page.suppressed_businesses += 1
                    continue
            contacts = kept

        visible.append((business, contacts))

    rows: list[dict[str, Any]] = []
    for business, contacts in visible:
        if not _matches_contact_filters(contacts, query):
            continue
        row = build_row(business, contacts)
        if not _matches_search(business, contacts, row, query.search):
            continue
        row["_business_id"] = str(business.id)
        row["_run_id"] = str(business.run_id)
        row["_place_id"] = business.place_id
        rows.append(row)

    if query.collapse_place_id:
        before = len(rows)
        rows = _collapse_on_place_id(rows)
        page.collapsed = before - len(rows)

    rows.sort(key=_sort_key(query.sort, query.descending), reverse=query.descending)

    page.total = len(rows)
    page.cities = _distinct(rows, "city")
    page.categories = _distinct(rows, "category")

    start = max(query.offset, 0)
    page.rows = rows[start : start + query.limit] if query.limit else rows[start:]
    return page


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def _select_businesses(session: Session, query: ResultQuery) -> Sequence[Business]:
    statement = select(Business).options(selectinload(Business.contacts))
    if query.run_ids:
        statement = statement.where(Business.run_id.in_(query.run_ids))
    if query.min_score is not None:
        # NULL is excluded here on purpose: an unscored business has not failed
        # the bar, it has not been measured against it. Asking for "score ≥ 60"
        # and being shown rows with no score at all would be the §10.2 "missing
        # is not zero" rule inverted.
        statement = statement.where(Business.lead_score >= query.min_score)
    return session.execute(statement).scalars().all()


def _ranked_phones(contacts: Iterable[Contact]) -> list[Contact]:
    return [
        c for c in contacts if c.kind == ContactKind.PHONE and c.rank is not None
    ]


def _matches_contact_filters(contacts: Sequence[Contact], query: ResultQuery) -> bool:
    """§13's contact-level filters, read against the numbers actually shown.

    Each is "does this business have a visible ranked number matching?" — an
    unranked contact is one §3.3 excluded or a §10.1 duplicate provenance row,
    and filtering on a number the operator cannot see would return rows that
    look, on screen, like they do not match.
    """
    phones = _ranked_phones(contacts)

    if query.whatsapp and not any(c.wa_label in query.whatsapp for c in phones):
        return False
    if query.line_types and not any(c.line_type in query.line_types for c in phones):
        return False
    if query.sources and not any(c.source in query.sources for c in contacts):
        return False
    if query.has_owner_name is not None:
        has_name = any(c.person_name for c in phones)
        if has_name is not query.has_owner_name:
            return False
    return True


def _matches_search(
    business: Business,
    contacts: Sequence[Contact],
    row: dict[str, Any],
    search: str | None,
) -> bool:
    """§13's free-text box.

    Spans the number columns as well as the name ones: pasting a phone number in
    to find out which business it belongs to is the query an operator actually
    runs, and the §16 hand-check is 50 rounds of exactly that.
    """
    if not search:
        return True
    needle = search.strip().lower()
    if not needle:
        return True

    haystack = [
        business.name,
        business.address,
        business.area,
        business.city,
        business.website,
        row.get("contact_person"),
    ]
    haystack.extend(c.value_e164 or c.value_raw for c in contacts)
    digits = _national_digits(needle)
    for value in haystack:
        if not value:
            continue
        lowered = str(value).lower()
        if needle in lowered:
            return True
        if digits and len(digits) >= 4 and digits in _national_digits(lowered):
            return True
    return False


def _national_digits(value: str) -> str:
    """Reduce a number to its PK national significant digits.

    The operator has the number written down as ``0300 5500940`` and we store
    ``+923005500940``; a plain digit-substring test never matches those, because
    the trunk ``0`` and the country code ``92`` occupy the same position. §9.2
    already draws this distinction for parsing — the search box needs it for the
    same reason, and a partial number the operator half-remembers still has to
    hit, so this stays a substring test rather than a parse.
    """
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits.startswith("92"):
        return digits[2:]
    return digits.removeprefix("0")


# --------------------------------------------------------------------------- #
# §10.1 read-side union
# --------------------------------------------------------------------------- #


def _collapse_on_place_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One table from many runs, non-destructively (§10.1's Phase 5 note).

    The four Lahore × salon runs hold 232 ``place_id`` values that are 72 real
    businesses. Collapsing them at *write* time would delete three runs and
    destroy the record §16's "validate by re-running" needs; collapsing at read
    time answers the operator's actual want and leaves every row in place.

    The winner is the best-scored row, then the most recently scraped — the run
    that enriched a business knows more about it than the run that only
    discovered it, and §10.2 measured that gap as 22 qualified leads against 0.
    A business with no ``place_id`` cannot be matched this way and is always
    kept: dropping it would be a merge asserted on no evidence.
    """
    best: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []

    for row in rows:
        place_id = row.get("_place_id")
        if not place_id:
            out.append(row)
            continue
        incumbent = best.get(place_id)
        if incumbent is None or _collapse_key(row) > _collapse_key(incumbent):
            best[place_id] = row

    out.extend(best.values())
    return out


def _collapse_key(row: dict[str, Any]) -> tuple:
    scraped = row.get("scraped_at")
    return (
        row.get("lead_score") or -1,
        row.get("phone_count") or 0,
        scraped.timestamp() if scraped is not None else 0.0,
    )


# --------------------------------------------------------------------------- #
# Sorting
# --------------------------------------------------------------------------- #


def _sort_key(column: str, descending: bool):
    """Sort with nulls last, in **both** directions.

    A missing value is not a low one (§10.2), so it does not belong at the top of
    ``lead_score DESC`` *or* at the top of ``lead_score ASC`` — "unknown" goes to
    the end of the table whichever way the operator clicked.

    The presence flag is therefore inverted with the direction rather than fixed:
    ``sort(reverse=True)`` flips the whole key, so present rows need the winning
    flag under descending and the losing one under ascending to end up in the
    same place. Absent rows all share one placeholder value, so two of them never
    compare a real value against a sentinel.
    """
    key = column if column in SORTABLE else DEFAULT_SORT
    present, absent = (1, 0) if descending else (0, 1)

    def _key(row: dict[str, Any]) -> tuple:
        value = row.get(key)
        if value is None:
            return (absent, 0)
        if isinstance(value, str):
            return (present, value.lower())
        if hasattr(value, "timestamp"):
            return (present, value.timestamp())
        return (present, value)

    return _key


def _distinct(rows: Sequence[dict[str, Any]], column: str) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for row in rows:
        value = row.get(column)
        if value:
            seen.setdefault(str(value), None)
    return tuple(seen)


def resolve_runs(session: Session, run_ids: Iterable[uuid.UUID]) -> list[Run]:
    ids = list(run_ids)
    if not ids:
        return []
    return list(
        session.execute(select(Run).where(Run.id.in_(ids))).scalars()
    )
