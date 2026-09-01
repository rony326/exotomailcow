import vobject

from app.graph.models import GraphContact


def graph_contact_to_vcard(contact: GraphContact) -> bytes:
    card = vobject.vCard()
    card.add("uid").value = contact.id
    card.add("fn").value = contact.display_name
    card.add("n").value = vobject.vcard.Name(family=contact.display_name)

    for email in contact.email_addresses:
        field = card.add("email")
        field.value = email
        field.type_param = "INTERNET"

    for phone in contact.business_phones:
        field = card.add("tel")
        field.value = phone
        field.type_param = "WORK"

    if contact.mobile_phone:
        field = card.add("tel")
        field.value = contact.mobile_phone
        field.type_param = "CELL"

    if contact.company_name:
        card.add("org").value = [contact.company_name]

    return card.serialize().encode("utf-8")
