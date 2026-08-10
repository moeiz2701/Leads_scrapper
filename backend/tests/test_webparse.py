"""§5.2 page parsing — every extraction shape, against markup that exists.

The HTML in these tests is deliberately in the idiom of real Pakistani SMB
sites: WordPress "Click to Chat" widgets, Cloudflare-obfuscated emails, hand
written JSON-LD, numbers printed as free text in a footer.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from leadscraper.core.webparse import (
    decode_body,
    decode_cfemail,
    extract_emails,
    extract_jsonld,
    extract_socials,
    extract_tel_numbers,
    extract_widget_numbers,
    find_crawl_targets,
    parse_page,
    visible_text,
)


def _tree(html: str) -> HTMLParser:
    return HTMLParser(html)


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #


def test_charset_from_content_type_wins() -> None:
    body = "Café".encode("windows-1252")
    assert decode_body(body, "text/html; charset=windows-1252") == "Café"


def test_meta_charset_is_used_when_the_header_is_silent() -> None:
    body = b'<meta charset="windows-1256">' + "م".encode("windows-1256")
    assert decode_body(body, None).endswith("م")


def test_undecodable_bytes_still_return_text() -> None:
    """A mislabelled page must still give up its phone number, not raise."""
    text = decode_body(b"\xff\xfe phone 0300-1234567", "text/html; charset=utf-8")
    assert "0300-1234567" in text


def test_unknown_encoding_label_falls_through() -> None:
    assert decode_body(b"hello", "text/html; charset=not-a-real-charset") == "hello"


# --------------------------------------------------------------------------- #
# Visible text (§9.1 runs over this, so what it strips matters)
# --------------------------------------------------------------------------- #


def test_script_contents_never_reach_the_phone_regex() -> None:
    """An analytics blob is full of long digit runs. If they survive into the
    text pass, the run harvests tracking IDs as landlines — a fabricated lead,
    which §10.2 then scores and ranks."""
    html = """
      <html><body>
        <script>var gtag_id = "923001234567"; var x = "0300-1234567";</script>
        <style>.a{content:"03211234567"}</style>
        <p>Call us on 0333-9876543</p>
      </body></html>
    """
    extract = parse_page("https://x.pk/", html)
    assert [p.e164 for p in extract.text_phones] == ["+923339876543"]


def test_visible_text_keeps_separators_between_blocks() -> None:
    text = visible_text(_tree("<body><p>Salon X</p><p>Lahore</p></body>"))
    assert "Salon X" in text and "Lahore" in text


# --------------------------------------------------------------------------- #
# §5.2 item 1 — wa.me links
# --------------------------------------------------------------------------- #


def test_wa_link_inside_an_inline_script_is_found() -> None:
    """wa.me URLs live in onclick handlers and JS config objects at least as
    often as in hrefs, and those are exactly what visible_text() throws away —
    so the wa pass reads raw markup, not the stripped tree."""
    html = """<body><script>
        var waUrl = "https://api.whatsapp.com/send?phone=923001234567&text=Hi";
    </script></body>"""
    assert parse_page("https://x.pk/", html).wa_numbers == ["+923001234567"]


def test_wa_link_in_an_href_is_found() -> None:
    html = '<body><a href="//wa.me/923339876543">WhatsApp</a></body>'
    assert parse_page("https://x.pk/", html).wa_numbers == ["+923339876543"]


# --------------------------------------------------------------------------- #
# §5.2 item 2 — chat widgets
# --------------------------------------------------------------------------- #


def test_click_to_chat_widget_data_phone_is_extracted() -> None:
    html = '<body><div class="ht-ctc ht-ctc-chat" data-phone="03001234567"></div></body>'
    assert extract_widget_numbers(_tree(html)) == ["+923001234567"]


def test_a_bare_data_phone_is_not_a_whatsapp_widget() -> None:
    """§9.3 scores a widget at 0.95 — `confirmed`. Plenty of themes put
    `data-phone` on an ordinary click-to-call button, and treating one of those
    as WhatsApp evidence would print `confirmed` next to a landline."""
    html = '<body><a class="call-button" data-phone="042-35771025">Call</a></body>'
    assert extract_widget_numbers(_tree(html)) == []


def test_widget_hint_may_sit_on_an_ancestor() -> None:
    html = """<body><div id="whatsapp-button"><span><i data-number="0321 1234567">
        </i></span></div></body>"""
    assert extract_widget_numbers(_tree(html)) == ["+923211234567"]


def test_widget_hint_beyond_the_ancestor_window_is_not_trusted() -> None:
    html = """<body><div class="whatsapp-float"><div><div><div>
        <span data-phone="03001234567"></span></div></div></div></div></body>"""
    assert extract_widget_numbers(_tree(html)) == []


def test_invalid_widget_number_is_dropped_not_guessed() -> None:
    html = '<body><div class="wa-widget" data-phone="not a number"></div></body>'
    assert extract_widget_numbers(_tree(html)) == []


# --------------------------------------------------------------------------- #
# §5.2 item 3 — tel:
# --------------------------------------------------------------------------- #


def test_tel_links_are_parsed_and_deduped() -> None:
    html = """<body>
      <a href="tel:+92-42-35771025">Call</a>
      <a href="tel:00924235771025">Call again</a>
      <a href="callto:03001234567">Mobile</a>
    </body>"""
    assert [p.e164 for p in extract_tel_numbers(_tree(html))] == [
        "+924235771025",
        "+923001234567",
    ]


def test_tel_link_query_string_is_stripped() -> None:
    html = '<body><a href="tel:03001234567?ext=4">Call</a></body>'
    assert [p.e164 for p in extract_tel_numbers(_tree(html))] == ["+923001234567"]


# --------------------------------------------------------------------------- #
# §5.2 item 5 — email and socials
# --------------------------------------------------------------------------- #


def test_mailto_and_free_text_emails_are_both_collected() -> None:
    html = (
        '<body><a href="mailto:info@salonx.pk?subject=Hi">Mail</a>'
        "<p>bookings@salonx.pk</p></body>"
    )
    tree = _tree(html)
    text = visible_text(tree)
    assert extract_emails(tree, text) == ["info@salonx.pk", "bookings@salonx.pk"]


def test_cloudflare_obfuscated_email_is_decoded() -> None:
    """§5.3 records that UrduPoint obfuscates the same way, so Phase 6 inherits
    this. It is not circumvention — the page's own script does this to render
    the address to every visitor."""
    encoded = _cfemail("owner@salonx.pk", key=0x2A)
    html = (
        f'<body><a class="__cf_email__" data-cfemail="{encoded}">'
        "[email&#160;protected]</a></body>"
    )
    tree = _tree(html)
    assert extract_emails(tree, visible_text(tree)) == ["owner@salonx.pk"]


def test_toolchain_addresses_are_not_leads() -> None:
    html = """<body>
      <p>hello@example.com</p><p>abc@sentry.io</p><p>logo@2x.png</p>
      <p>real@salonx.com.pk</p>
    </body>"""
    tree = _tree(html)
    assert extract_emails(tree, visible_text(tree)) == ["real@salonx.com.pk"]


def test_social_profiles_are_captured_but_share_buttons_are_not() -> None:
    html = """<body>
      <a href="https://www.facebook.com/sharer/sharer.php?u=x">Share</a>
      <a href="https://www.facebook.com/SalonXLahore">Our page</a>
      <a href="https://instagram.com/salonx">Instagram</a>
    </body>"""
    facebook, instagram = extract_socials(_tree(html), "https://salonx.pk/")
    assert facebook == "https://www.facebook.com/SalonXLahore"
    assert instagram == "https://instagram.com/salonx"


def test_jsonld_sameas_fills_socials_when_the_page_has_no_links() -> None:
    html = """<body><script type="application/ld+json">
      {"@type":"LocalBusiness","name":"Salon X","telephone":"03001234567",
       "sameAs":["https://facebook.com/salonx","https://instagram.com/salonx"]}
    </script></body>"""
    extract = parse_page("https://salonx.pk/", html)
    assert extract.facebook_url == "https://facebook.com/salonx"
    assert extract.instagram_url == "https://instagram.com/salonx"


# --------------------------------------------------------------------------- #
# §5.2 item 6 — JSON-LD
# --------------------------------------------------------------------------- #


def test_jsonld_local_business_yields_phone_email_and_founder() -> None:
    html = """<body><script type="application/ld+json">
      {"@context":"https://schema.org","@type":"HealthAndBeautyBusiness",
       "name":"Salon X","telephone":"+92 300 1234567","email":"info@salonx.pk",
       "founder":{"@type":"Person","name":"Ayesha Khan"}}
    </script></body>"""
    blocks = extract_jsonld(_tree(html))
    assert len(blocks) == 1
    assert blocks[0].telephones == ("+92 300 1234567",)
    assert blocks[0].emails == ("info@salonx.pk",)
    assert blocks[0].people == (("Ayesha Khan", "founder"),)


def test_jsonld_graph_nesting_is_walked() -> None:
    html = """<body><script type="application/ld+json">
      {"@context":"https://schema.org","@graph":[
        {"@type":"WebSite","name":"x"},
        {"@type":["Organization","Store"],"name":"Salon X","telephone":"0300-1234567"}]}
    </script></body>"""
    blocks = extract_jsonld(_tree(html))
    assert [b.name for b in blocks] == ["Salon X"]


def test_jsonld_telephone_may_be_a_list() -> None:
    html = """<body><script type="application/ld+json">
      {"@type":"LocalBusiness","name":"X","telephone":["03001234567","042-35771025"]}
    </script></body>"""
    assert extract_jsonld(_tree(html))[0].telephones == ("03001234567", "042-35771025")


def test_one_malformed_jsonld_block_does_not_cost_the_others() -> None:
    """Hand-written JSON-LD with a trailing comma is extremely common. Losing
    the whole page's structured data to one bad block is not worth it."""
    html = """<body>
      <script type="application/ld+json">{"@type":"LocalBusiness", "name":,}</script>
      <script type="application/ld+json">{"@type":"LocalBusiness","name":"Salon X",
        "telephone":"03001234567"}</script>
    </body>"""
    assert [b.name for b in extract_jsonld(_tree(html))] == ["Salon X"]


def test_untyped_block_with_a_name_and_phone_still_counts() -> None:
    html = """<body><script type="application/ld+json">
      {"name":"Salon X","telephone":"03001234567"}
    </script></body>"""
    assert extract_jsonld(_tree(html))[0].telephones == ("03001234567",)


def test_a_person_block_alone_is_not_a_business() -> None:
    html = """<body><script type="application/ld+json">
      {"@type":"Person","name":"Ayesha Khan"}
    </script></body>"""
    assert extract_jsonld(_tree(html)) == []


# --------------------------------------------------------------------------- #
# §5.2 crawl budget
# --------------------------------------------------------------------------- #


def test_contact_pages_rank_above_about_pages() -> None:
    html = """<body>
      <a href="/about-us/">About Us</a>
      <a href="/contact/">Contact</a>
    </body>"""
    assert find_crawl_targets(_tree(html), "https://salonx.pk/") == [
        "https://salonx.pk/contact/",
        "https://salonx.pk/about-us/",
    ]


def test_link_text_finds_a_contact_page_with_an_unhelpful_path() -> None:
    html = '<body><a href="/pages/p2">Get in touch</a></body>'
    assert find_crawl_targets(_tree(html), "https://salonx.pk/") == [
        "https://salonx.pk/pages/p2"
    ]


def test_offsite_links_and_assets_are_not_crawl_targets() -> None:
    html = """<body>
      <a href="https://other.pk/contact">Contact them</a>
      <a href="/brochure-about.pdf">About (PDF)</a>
      <a href="mailto:a@b.pk">Contact</a>
      <a href="#contact">Contact</a>
    </body>"""
    assert find_crawl_targets(_tree(html), "https://salonx.pk/") == []


def test_crawl_targets_are_deduped_and_capped() -> None:
    links = "".join(f'<a href="/contact-{i}">Contact {i}</a>' for i in range(6))
    html = f'<body>{links}<a href="/contact-0/">Contact 0 again</a></body>'
    targets = find_crawl_targets(_tree(html), "https://salonx.pk/")
    assert len(targets) == 3
    assert len(set(targets)) == 3


def test_the_homepage_is_never_its_own_crawl_target() -> None:
    html = '<body><a href="/">Home</a><a href="https://salonx.pk">Home</a></body>'
    assert find_crawl_targets(_tree(html), "https://salonx.pk/") == []


# --------------------------------------------------------------------------- #
# Whole-page assembly
# --------------------------------------------------------------------------- #


def test_parse_page_gathers_every_shape_at_once() -> None:
    html = """<html><body>
      <a href="https://wa.me/923001234567">WhatsApp us</a>
      <div class="ht-ctc" data-phone="03211234567"></div>
      <a href="tel:04235771025">042 3577 1025</a>
      <a href="mailto:info@salonx.pk">Email</a>
      <a href="/contact-us">Contact</a>
      <p>Bridal bookings: 0333-9876543</p>
      <script type="application/ld+json">{"@type":"BeautySalon","name":"Salon X",
        "telephone":"0345-1112222"}</script>
    </body></html>"""
    extract = parse_page("https://salonx.pk/", html)

    assert extract.wa_numbers == ["+923001234567"]
    assert extract.widget_numbers == ["+923211234567"]
    assert [p.e164 for p in extract.tel_numbers] == ["+924235771025"]
    assert [p.e164 for p in extract.jsonld_numbers] == ["+923451112222"]
    assert "+923339876543" in [p.e164 for p in extract.text_phones]
    assert extract.emails == ["info@salonx.pk"]
    assert extract.crawl_targets == ["https://salonx.pk/contact-us"]
    assert extract.has_any_contact


def test_an_empty_page_yields_nothing_and_says_so() -> None:
    extract = parse_page("https://salonx.pk/", "<html><body><h1>Coming soon</h1></body></html>")
    assert not extract.has_any_contact


def test_parse_page_accepts_bytes_with_a_declared_charset() -> None:
    body = '<body><p>Call 0300-1234567</p></body>'.encode("windows-1252")
    extract = parse_page("https://x.pk/", body, "text/html; charset=windows-1252")
    assert [p.e164 for p in extract.text_phones] == ["+923001234567"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _cfemail(email: str, key: int) -> str:
    return bytes([key] + [ord(c) ^ key for c in email]).hex()


def test_cfemail_roundtrip_helper_matches_the_decoder() -> None:
    assert decode_cfemail(_cfemail("a@b.pk", 0x7F)) == "a@b.pk"


def test_cfemail_rejects_garbage() -> None:
    assert decode_cfemail("zzzz") is None
    assert decode_cfemail("") is None
