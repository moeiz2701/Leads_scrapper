"""§9.3 evidence scoring across a whole crawled site.

The unit under test is the *site*, not the page: the reason §5.2 has a 4-page
crawl budget at all is that the contact page routinely proves a number the
homepage merely printed.
"""

from __future__ import annotations

from leadscraper.core.site_evidence import (
    MAX_PHONES_PER_SITE,
    PhoneOrigin,
    build_site_evidence,
)
from leadscraper.core.webparse import parse_page
from leadscraper.enums import Attribution, LineType, PersonRole, WhatsAppLabel

SITE = "https://salonx.pk/"


def _pages(*html_by_url: tuple[str, str]):
    return [parse_page(url, html) for url, html in html_by_url]


def _find(evidence, e164):
    return next(p for p in evidence.phones if p.e164 == e164)


# --------------------------------------------------------------------------- #
# The core of §5.2: turning a `likely` into a `confirmed`
# --------------------------------------------------------------------------- #


def test_a_wa_link_on_the_contact_page_confirms_a_number_from_the_homepage() -> None:
    """This is what Phase 3 exists for. Maps gives you 0300-1234567 and §9.3
    scores it 0.60 — *likely*. The business's own contact page carrying the
    matching wa.me link is the 1.00 row of the §9.3 table."""
    pages = _pages(
        (SITE, "<body><p>Call 0300-1234567</p><a href='/contact'>Contact</a></body>"),
        ("https://salonx.pk/contact", "<body><a href='https://wa.me/923001234567'>Chat</a></body>"),
    )
    finding = _find(build_site_evidence(SITE, pages), "+923001234567")

    assert finding.evidence.label is WhatsAppLabel.CONFIRMED
    assert finding.evidence.score == 1.00
    # Provenance: the proof came from the contact page, not the homepage.
    assert finding.evidence_url == "https://salonx.pk/contact"
    assert finding.source_url == SITE


def test_a_number_seen_by_several_shapes_is_still_one_contact() -> None:
    pages = _pages(
        (SITE, "<body><a href='tel:03001234567'>Call</a></body>"),
        ("https://salonx.pk/c", "<body><a href='https://wa.me/923001234567'>W</a></body>"),
    )
    evidence = build_site_evidence(SITE, pages)
    assert len(evidence.phones) == 1
    assert evidence.phones[0].origin is PhoneOrigin.WA_LINK


def test_a_page_that_never_mentions_whatsapp_does_not_lower_the_score() -> None:
    """Absence of evidence is not evidence of absence — the best signal on any
    page wins, so an about page with a bare tel: link cannot undo a wa.me link."""
    pages = _pages(
        (SITE, "<body><a href='https://wa.me/923001234567'>Chat</a></body>"),
        ("https://salonx.pk/about", "<body><a href='tel:03001234567'>Call</a></body>"),
    )
    assert _find(build_site_evidence(SITE, pages), "+923001234567").evidence.score == 1.00


# --------------------------------------------------------------------------- #
# The §9.3 ladder
# --------------------------------------------------------------------------- #


def test_widget_scores_confirmed_and_text_proximity_scores_likely() -> None:
    pages = _pages(
        (
            SITE,
            """<body>
              <div class="ht-ctc" data-phone="03001234567"></div>
              <p>WhatsApp us on 0321-1234567</p>
              <p>Reception 0333-9876543</p>
            </body>""",
        )
    )
    evidence = build_site_evidence(SITE, pages)
    assert _find(evidence, "+923001234567").evidence.score == 0.95
    assert _find(evidence, "+923211234567").evidence.score == 0.75
    assert _find(evidence, "+923339876543").evidence.score == 0.60


def test_a_landline_never_earns_the_mobile_baseline() -> None:
    """§9.2: landlines have no WhatsApp likelihood. A tel: link to a Lahore
    landline is a good contact and a `no` on the WhatsApp column."""
    pages = _pages((SITE, "<body><a href='tel:04235771025'>Call</a></body>"))
    finding = _find(build_site_evidence(SITE, pages), "+924235771025")
    assert finding.line_type is LineType.LANDLINE
    assert finding.evidence.label is WhatsAppLabel.NO
    assert finding.confidence == 0.85  # still a high-confidence *contact*


def test_a_landline_published_as_a_wa_link_is_confirmed_anyway() -> None:
    """Businesses do register landlines on WhatsApp Business. Published evidence
    outranks the §9.2 prefix table — that table is a prior, not a veto."""
    pages = _pages((SITE, "<body><a href='https://wa.me/924235771025'>Chat</a></body>"))
    assert _find(build_site_evidence(SITE, pages), "+924235771025").is_confirmed


# --------------------------------------------------------------------------- #
# Contact confidence (§10.2) — a different axis from WhatsApp evidence
# --------------------------------------------------------------------------- #


def test_structured_data_outranks_free_text_for_contact_confidence() -> None:
    pages = _pages(
        (
            SITE,
            """<body><p>Maybe 0333-9876543</p>
            <script type="application/ld+json">{"@type":"LocalBusiness","name":"X",
              "telephone":"0300-1234567"}</script></body>""",
        )
    )
    evidence = build_site_evidence(SITE, pages)
    assert _find(evidence, "+923001234567").confidence == 0.90
    assert _find(evidence, "+923339876543").confidence == 0.60


def test_findings_are_ordered_best_evidence_first() -> None:
    pages = _pages(
        (
            SITE,
            """<body>
              <p>Landline 042-35771025</p>
              <p>Mobile 0333-9876543</p>
              <a href="https://wa.me/923001234567">Chat</a>
            </body>""",
        )
    )
    scores = [p.evidence.score for p in build_site_evidence(SITE, pages).phones]
    assert scores == sorted(scores, reverse=True)


def test_a_listings_page_cannot_flood_one_business_with_numbers() -> None:
    numbers = "".join(f"<p>0300-123{i:04d}</p>" for i in range(30))
    pages = _pages((SITE, f"<body>{numbers}</body>"))
    assert len(build_site_evidence(SITE, pages).phones) == MAX_PHONES_PER_SITE


# --------------------------------------------------------------------------- #
# §8 person attribution — only inside one structured record
# --------------------------------------------------------------------------- #


def test_a_founder_named_beside_the_phone_is_linked_to_it() -> None:
    pages = _pages(
        (
            SITE,
            """<body><script type="application/ld+json">
              {"@type":"BeautySalon","name":"Salon X","telephone":"0300-1234567",
               "founder":{"@type":"Person","name":"Ayesha Khan"}}
            </script></body>""",
        )
    )
    finding = _find(build_site_evidence(SITE, pages), "+923001234567")
    assert finding.person_name == "Ayesha Khan"
    assert finding.person_role is PersonRole.OWNER
    assert finding.attribution is Attribution.LINKED


def test_a_founder_in_a_different_block_is_never_joined_to_the_phone() -> None:
    """§8: "Never fabricate the join." Two adjacent JSON-LD blocks are two
    records, and a name in one says nothing about a number in the other."""
    pages = _pages(
        (
            SITE,
            """<body>
              <script type="application/ld+json">{"@type":"Organization",
                "name":"Holding","founder":{"@type":"Person","name":"Ayesha Khan"}}</script>
              <script type="application/ld+json">{"@type":"LocalBusiness",
                "name":"Salon X","telephone":"0300-1234567"}</script>
            </body>""",
        )
    )
    assert _find(build_site_evidence(SITE, pages), "+923001234567").person_name is None


def test_an_employee_is_not_promoted_to_owner() -> None:
    """§8 gives `employee` no role. Capturing the name is fine; calling that
    person the owner is the fabrication the section forbids."""
    pages = _pages(
        (
            SITE,
            """<body><script type="application/ld+json">
              {"@type":"LocalBusiness","name":"Salon X","telephone":"0300-1234567",
               "employee":{"@type":"Person","name":"Bilal"}}
            </script></body>""",
        )
    )
    assert _find(build_site_evidence(SITE, pages), "+923001234567").person_name is None


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


def test_emails_and_socials_union_across_pages_in_order() -> None:
    pages = _pages(
        (SITE, "<body><a href='mailto:info@salonx.pk'>a</a></body>"),
        (
            "https://salonx.pk/contact",
            """<body><a href='mailto:info@salonx.pk'>a</a>
               <a href='mailto:bookings@salonx.pk'>b</a>
               <a href='https://instagram.com/salonx'>ig</a></body>""",
        ),
    )
    evidence = build_site_evidence(SITE, pages)
    assert evidence.emails == ["info@salonx.pk", "bookings@salonx.pk"]
    assert evidence.instagram_url == "https://instagram.com/salonx"


def test_a_site_with_nothing_on_it_reports_nothing() -> None:
    evidence = build_site_evidence(SITE, _pages((SITE, "<body><h1>Coming soon</h1></body>")))
    assert not evidence.has_findings
    assert evidence.pages_parsed == 1


def test_no_pages_at_all_is_not_a_crash() -> None:
    evidence = build_site_evidence(SITE, [])
    assert evidence.phones == [] and evidence.pages_parsed == 0


def test_confirmed_phones_is_the_subset_the_operator_cares_about() -> None:
    pages = _pages(
        (
            SITE,
            """<body><a href='https://wa.me/923001234567'>W</a>
               <p>Also 0333-9876543</p></body>""",
        )
    )
    evidence = build_site_evidence(SITE, pages)
    assert [p.e164 for p in evidence.confirmed_phones] == ["+923001234567"]
