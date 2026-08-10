"""§15 — the suppression list and the bulk-delete path.

§15 requires both in v1 and warns that "retrofitting it is painful". The table
has existed since Phase 1 and **nothing had ever queried it**; this is the code
that does.

**Deleting is not removing, and that is the whole design of this module.**
Deleting a business row honours a removal request until the next run re-scrapes
the same salon from the same Maps listing and puts it straight back. §15's
requirement is that a removal "goes in permanently, and it survives re-runs", so
the durable artifact is the ``do_not_contact`` entry and the row deletion is the
cosmetic half. They happen in one transaction, suppression first: if the
suppression cannot be written, nothing is deleted.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from leadscraper.api.deps import SessionDep
from leadscraper.api.schemas import (
    BulkDeleteRequest,
    BulkDeleteResult,
    SuppressionCreate,
    SuppressionEntry,
)
from leadscraper.core.textnorm import registrable_domain
from leadscraper.db.models import Business, Contact, DoNotContact
from leadscraper.enums import ContactKind
from leadscraper.logging import get_logger

router = APIRouter(prefix="/api/do-not-contact", tags=["compliance"])

log = get_logger(__name__)


@router.get("", response_model=list[SuppressionEntry])
def list_suppressions(session: SessionDep, limit: int = 500) -> list[DoNotContact]:
    return list(
        session.execute(
            select(DoNotContact).order_by(DoNotContact.created_at.desc()).limit(limit)
        ).scalars()
    )


@router.post("", response_model=SuppressionEntry, status_code=status.HTTP_201_CREATED)
def add_suppression(payload: SuppressionCreate, session: SessionDep) -> DoNotContact:
    if not (payload.value_e164 or payload.email or payload.domain):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "A suppression needs a number, an email or a domain to match on.",
        )
    entry = DoNotContact(**payload.model_dump())
    session.add(entry)
    session.commit()
    session.refresh(entry)
    log.info("suppression.added", domain=entry.domain, has_number=bool(entry.value_e164))
    return entry


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_suppression(entry_id: str, session: SessionDep) -> None:
    """Undo a suppression added in error.

    Deliberately does not restore anything: the business rows a bulk delete
    removed are gone, and the next run rediscovers them. Reversing the *list* is
    all this can honestly offer.
    """
    entry = session.get(DoNotContact, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such suppression entry.")
    session.delete(entry)
    session.commit()


@router.post("/bulk-delete", response_model=BulkDeleteResult, tags=["compliance"])
def bulk_delete(payload: BulkDeleteRequest, session: SessionDep) -> BulkDeleteResult:
    """§13 Screen 3's "bulk select → delete", done so it survives a re-run.

    Every number on the selected businesses goes onto the suppression list, plus
    the business's domain where it has one, and only then are the rows deleted.
    Contacts cascade with the business (§11's ``ON DELETE CASCADE``).
    """
    businesses = list(
        session.execute(
            select(Business).where(Business.id.in_(payload.business_ids))
        ).scalars()
    )
    if not businesses:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No matching businesses.")

    result = BulkDeleteResult(
        businesses_deleted=0, contacts_deleted=0, suppressions_added=0
    )

    if not payload.suppress:
        # Allowed, because deleting a test run is a real need. Never silent: a
        # deletion that does not suppress will be undone by the next run of the
        # same city and category, and the operator has to know that.
        result.warnings.append(
            "Deleted without suppressing. These businesses will be rediscovered by "
            "the next run of the same city × category — §15 requires the "
            "do_not_contact entry, not the row deletion, to make a removal stick."
        )

    existing_numbers = {
        value
        for (value,) in session.execute(
            select(DoNotContact.value_e164).where(DoNotContact.value_e164.is_not(None))
        )
    }
    existing_domains = {
        value
        for (value,) in session.execute(
            select(DoNotContact.domain).where(DoNotContact.domain.is_not(None))
        )
    }

    for business in businesses:
        contacts = list(business.contacts)
        result.contacts_deleted += len(contacts)

        if payload.suppress:
            for contact in contacts:
                if contact.kind != ContactKind.PHONE or not contact.value_e164:
                    continue
                if contact.value_e164 in existing_numbers:
                    continue
                existing_numbers.add(contact.value_e164)
                session.add(
                    DoNotContact(
                        value_e164=contact.value_e164,
                        reason=payload.reason or "bulk delete (§13 Screen 3)",
                        added_by=payload.added_by,
                    )
                )
                result.numbers_suppressed.append(contact.value_e164)
                result.suppressions_added += 1

            domain = registrable_domain(business.website) if business.website else None
            if domain and domain not in existing_domains:
                existing_domains.add(domain)
                session.add(
                    DoNotContact(
                        domain=domain,
                        reason=payload.reason or "bulk delete (§13 Screen 3)",
                        added_by=payload.added_by,
                    )
                )
                result.domains_suppressed.append(domain)
                result.suppressions_added += 1

        session.delete(business)
        result.businesses_deleted += 1

    # One transaction. A crash between the suppression and the delete would
    # otherwise leave a business the operator believes is removed.
    session.commit()

    log.info(
        "suppression.bulk_delete",
        businesses=result.businesses_deleted,
        suppressions=result.suppressions_added,
        suppressed=payload.suppress,
    )
    return result


def suppressed_contact_count(session) -> int:
    """How many live contacts the list currently hides. For the Settings screen.

    Counts rows that are still in the database and merely invisible — a
    suppression added by hand hides contacts without deleting anything, and the
    operator should be able to see the size of that gap.
    """
    numbers = {
        value
        for (value,) in session.execute(
            select(DoNotContact.value_e164).where(DoNotContact.value_e164.is_not(None))
        )
    }
    if not numbers:
        return 0
    return session.scalar(
        select(func.count(Contact.id)).where(Contact.value_e164.in_(numbers))
    ) or 0
