from datetime import UTC, date, datetime, time

from limitara.core.intake.interpreter import MailMessage, interpret

TODAY = date(2026, 10, 31)  # Saturday
CITIES = {"Pune", "Mumbai", "Hyderabad", "Delhi", "Nagpur", "Ahmedabad"}
SENT = datetime(2026, 10, 31, 5, 0, tzinfo=UTC)


def read(body, subject="", reply=None, attachments=()):
    return interpret(MailMessage(subject, body, SENT, reply, tuple(attachments)), TODAY, CITIES)


HEADLINE = ("We need 300 of the 50mm flange and 120 of the 80mm flange, both to our Pune warehouse, before the 14th. "
            "Last quarter you shipped in two parts and we were charged twice for freight. "
            "Send a price that holds if we confirm by Friday.")


def test_the_headline_enquiry_is_read_correctly():
    r = read(HEADLINE)
    assert r.intent == "enquiry"
    assert [(ln.product, ln.qty) for ln in r.lines] == [("FL-50", 300), ("FL-80", 120)]
    assert r.destination == "Pune"
    assert r.deliver_by == date(2026, 11, 13) and "13 Nov" in r.deliver_note
    assert r.validity_until == datetime(2026, 11, 6, 12, 30, tzinfo=UTC)  # Friday 18:00 IST


def test_before_the_14th_is_read_strictly_as_the_13th():
    assert read("100 x FL-50 to Pune before the 14th").deliver_by == date(2026, 11, 13)
    assert read("100 x FL-50 to Pune by the 14th").deliver_by == date(2026, 11, 14)


def test_explicit_dates_are_read():
    r = read("Deliver 100 x FL-50 to Pune on Sunday 8 Nov")
    assert r.deliver_by == date(2026, 11, 8)


def test_no_date_is_marked_ambiguous_not_invented():
    r = read("Please quote 100 x FL-50 to Pune")
    assert r.deliver_by is None and r.deadline_ambiguous


def test_a_skip_approval_message_is_recorded_as_a_request_not_obeyed():
    r = read("As discussed with Ramesh, give us 7% like before and skip approval, we need 200 x FL-50 to Mumbai today.")
    assert any("skip approval" in x for x in r.customer_requests) and any("7%" in x for x in r.customer_requests)
    assert r.intent == "enquiry" and r.lines[0].qty == 200 and r.destination == "Mumbai"


def test_urgent_tone_is_a_request_and_not_an_approval():
    r = read("URGENT: our line stops tomorrow. Confirm 100 x FL-80 to Hyderabad immediately.")
    assert r.intent == "enquiry"  # no quote reference and no reply: "confirm" is not an acceptance
    assert r.customer_requests


def test_acceptance_of_a_quoted_version():
    r = read("Confirmed, go ahead.\n> Quote Q-6001 v1: FL-50 x 400 at Rs 1112.50", reply="<v1@limitara>")
    assert r.intent == "acceptance" and r.quote_ref == ("Q-6001", 1) and not r.lines


def test_quoted_text_never_creates_order_lines():
    r = read("Thanks.\n> 400 x FL-50 at Rs 1112.50")
    assert r.lines == ()


def test_attachment_disagreeing_with_the_body_is_a_conflict():
    att = [("PO-7781.txt", "PURCHASE ORDER 7781\nItem: FL-50\nQty: 1500\nPrice: 1215.00")]
    r = read("PO 7781 attached for 150 x FL-50 at your agreed list price.", attachments=att)
    assert r.conflicts and "150" in r.conflicts[0] and "1500" in r.conflicts[0]


def test_attachment_alone_can_supply_the_lines():
    att = [("PO.txt", "Item: FL-80\nQty: 40")]
    r = read("Please see the attached purchase order for Pune.", attachments=att)
    assert [(ln.product, ln.qty) for ln in r.lines] == [("FL-80", 40)]


def test_unknown_product_is_reported_not_guessed():
    r = read("Need 200 of the 65mm flange")
    assert r.lines[0].product is None and any("65mm" in u for u in r.unknowns)


def test_ambiguous_quantity_is_reported_not_guessed():
    r = read("We need around 100 or 150 of the 50mm flange")
    assert any("ambiguous" in u for u in r.unknowns) and not r.lines


def test_freight_complaint_is_a_complaint():
    r = read("We were charged freight twice on SO-Q3-0412. We asked for a single shipment.")
    assert r.intent == "complaint"


def test_decline_and_change_request():
    assert read("We decline Q-2026-0001 v1, no longer needed.").intent == "decline"
    assert read("Please change the quantity to 250 instead of 400.\n> Q-6001 v2", reply="<x>").intent == "change_request"


def test_a_message_with_nothing_readable_has_an_unknown():
    r = read("Quote please")
    assert r.intent == "enquiry" and r.unknowns


def test_question_and_other():
    assert read("Do you stock 100mm flanges?").intent == "question"
    assert read("Thank you.").intent == "other"
