from app.conversion.vcard import graph_contact_to_vcard
from app.graph.models import GraphContact


def test_graph_contact_to_vcard_contains_core_fields():
    contact = GraphContact(
        id="c1",
        last_modified_date_time=__import__("datetime").datetime(2026, 1, 1),
        display_name="Maria Muster",
        email_addresses=["maria@example.org"],
        business_phones=["+41 44 000 00 00"],
        mobile_phone="+41 79 000 00 00",
        company_name="Kirchgemeinde",
    )
    vcard_bytes = graph_contact_to_vcard(contact)
    text = vcard_bytes.decode("utf-8")

    assert "UID:c1" in text
    assert "FN:Maria Muster" in text
    assert "maria@example.org" in text
    assert "+41 79 000 00 00" in text
    assert "Kirchgemeinde" in text


def test_graph_contact_to_vcard_without_optional_fields():
    contact = GraphContact(
        id="c2",
        last_modified_date_time=__import__("datetime").datetime(2026, 1, 1),
        display_name="Ohne Firma",
    )
    vcard_bytes = graph_contact_to_vcard(contact)
    assert b"UID:c2" in vcard_bytes
