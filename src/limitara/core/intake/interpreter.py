"""Turns one email into a structured request. Pure and deterministic.

The result is data to be validated, never a command. Anything that sounds like an instruction
(skip approval, a discount, ship today) is recorded as a customer request and never acted on.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from limitara.clock import IST

SIZE_TO_PRODUCT = {"50": "FL-50", "80": "FL-80"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
WEEKDAYS = {d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}

QUOTE_REF = re.compile(r"(Q-[A-Z0-9]+(?:-\d+)?)\s*v(\d+)", re.I)
AMBIGUOUS_QTY = re.compile(r"(\d+)\s*(?:or|to)\s*(\d+)\s*(?:x|of)?\s*(?:the\s+)?(?:FL-?\d+|\d+\s*mm)", re.I)
QTY_FIRST = re.compile(r"(?P<qty>\d[\d,]*)(?:\s*(?:x|×)\s*|\s+(?:(?:nos\.?|pcs\.?|pieces|units)\s+)?(?:of\s+)?)(?:the\s+)?"
                       r"(?:FL-?\s?(?P<s1>\d+)|(?P<s2>\d+)\s*mm\s*flanges?)", re.I)
PRODUCT_FIRST = re.compile(r"FL-?\s?(?P<s1>\d+)\s*(?:x|×)\s*(?P<qty>\d[\d,]*)", re.I)
ATTACH_ITEM = re.compile(r"Item:\s*FL-?\s?(\d+)", re.I)
ATTACH_QTY = re.compile(r"Qty:\s*(\d[\d,]*)", re.I)

COMPLAINT = re.compile(r"charged\b.*\btwice|\bdispute|\bcomplain|overcharg|wrong invoice|\binvoice\b", re.I)
DECLINE = re.compile(r"\b(decline|cancel|no longer (?:need|want)|not (?:going to )?proceed|withdraw)\b", re.I)
ACCEPT = re.compile(r"\b(accept(?:ed)?|confirm(?:ed)?|go ahead|please proceed|we agree|happy to proceed)\b", re.I)
CHANGE = re.compile(r"\b(change|amend|instead of|increase|reduce|revise|make it)\b", re.I)
ENQUIRY_WORDS = re.compile(r"\b(quote|quotation|price|need|require|looking for|enquiry|rfq)\b", re.I)
REQUEST_PATTERNS = [
    re.compile(r"skip(?:ping)? (?:the )?approval|without (?:any )?approval|no approval|don'?t need approval", re.I),
    re.compile(r"\d+(?:\.\d+)?\s*%", re.I),
    re.compile(r"like before|same as last", re.I),
    re.compile(r"\btoday\b|\bimmediately\b|\basap\b|\burgent", re.I),
    re.compile(r"\bignore\b", re.I),
]


@dataclass(frozen=True)
class MailMessage:
    subject: str
    body: str
    sent_at: datetime
    in_reply_to: str | None = None
    attachments: tuple[tuple[str, str], ...] = ()   # (filename, text)


@dataclass(frozen=True)
class Line:
    product: str | None
    qty: int
    source: str
    size_text: str = ""


@dataclass(frozen=True)
class Interpretation:
    intent: str
    lines: tuple[Line, ...] = ()
    destination: str | None = None
    deliver_by: date | None = None
    deliver_note: str = ""
    deadline_ambiguous: bool = False
    validity_until: datetime | None = None
    quote_ref: tuple[str, int] | None = None
    customer_requests: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()


def _split(body: str) -> tuple[str, str]:
    new, quoted = [], []
    for line in body.splitlines():
        (quoted if line.lstrip().startswith(">") else new).append(line)
    return "\n".join(new), "\n".join(quoted)


def _int(text: str) -> int:
    return int(text.replace(",", ""))


def _parse_lines(text: str, source: str) -> tuple[list[Line], list[str]]:
    unknowns: list[str] = []
    for m in AMBIGUOUS_QTY.finditer(text):
        unknowns.append(f"ambiguous quantity: \"{m.group(0).strip()}\" could be {m.group(1)} or {m.group(2)}")
    stripped = AMBIGUOUS_QTY.sub(" ", text)
    found: list[Line] = []
    for pattern in (PRODUCT_FIRST, QTY_FIRST):
        for m in pattern.finditer(stripped):
            size = m.groupdict().get("s1") or m.groupdict().get("s2")
            product = SIZE_TO_PRODUCT.get(size)
            if product is None:
                unknowns.append(f"product not recognised: a {size}mm flange is not in our catalogue")
            line = Line(product, _int(m.group("qty")), source, f"{size}mm")
            if line not in found:
                found.append(line)
        stripped = PRODUCT_FIRST.sub(" ", stripped)
    return found, unknowns


def _parse_attachment(filename: str, text: str) -> list[Line]:
    item, qty = ATTACH_ITEM.search(text), ATTACH_QTY.search(text)
    if item and qty:
        size = item.group(1)
        return [Line(SIZE_TO_PRODUCT.get(size), _int(qty.group(1)), f"attachment {filename}", f"{size}mm")]
    return []


def _next_day_of_month(day: int, today: date) -> date | None:
    probe = today
    for _ in range(62):
        if probe.day == day:
            return probe
        probe += timedelta(days=1)
    return None


def _deadline(text: str, today: date) -> tuple[date | None, str, bool]:
    m = re.search(r"before (?:the )?(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))", text, re.I)
    if m:
        target = _next_day_of_month(int(m.group(1)), today)
        if target:
            strict = target - timedelta(days=1)
            return strict, f"\"before the {m.group(1)}th\" read as delivery on or before {strict.strftime('%d %b %Y')}", False
    m = re.search(r"\b(?:by|on|before)\s+(?:\w+day\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*", text, re.I)
    if m:
        day, month = int(m.group(1)), MONTHS[m.group(2).lower()[:3]]
        year = today.year if (month, day) >= (today.month, today.day) else today.year + 1
        word = m.group(0).split()[0].lower()
        target = date(year, month, day)
        if word == "before":
            target -= timedelta(days=1)
        return target, f"\"{m.group(0).strip()}\" read as delivery on or before {target.strftime('%d %b %Y')}", False
    m = re.search(r"\bby (?:the )?(\d{1,2})(?:st|nd|rd|th)\b", text, re.I)
    if m:
        target = _next_day_of_month(int(m.group(1)), today)
        if target:
            return target, f"\"by the {m.group(1)}th\" read as delivery on or before {target.strftime('%d %b %Y')}", False
    if re.search(r"\btoday\b", text, re.I):
        return today, "\"today\" read as delivery today", False
    return None, "", True


def _validity(text: str, today: date, cutoff: time) -> datetime | None:
    m = re.search(r"(?:confirm|valid|hold|open|accept)\w*(?:\s+\w+){0,3}\s+(?:by|until|till|before)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", text, re.I)
    if not m:
        return None
    target_weekday = WEEKDAYS[m.group(1).lower()]
    probe = today
    while probe.weekday() != target_weekday:
        probe += timedelta(days=1)
    return datetime.combine(probe, cutoff, tzinfo=IST).astimezone(UTC)


def _requests(text: str) -> tuple[str, ...]:
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        s = sentence.strip()
        if s and any(p.search(s) for p in REQUEST_PATTERNS) and s not in out:
            out.append(s)
    return tuple(out)


def interpret(msg: MailMessage, today: date, cities: set[str], validity_cutoff: time = time(18, 0)) -> Interpretation:
    new, quoted = _split(msg.body)
    ref = None
    for source in (msg.subject, new, quoted):
        m = QUOTE_REF.search(source)
        if m:
            ref = (m.group(1).upper(), int(m.group(2)))
            break
    lines, unknowns = _parse_lines(new, "body")
    conflicts: list[str] = []
    for filename, text in msg.attachments:
        for att in _parse_attachment(filename, text):
            same = [ln for ln in lines if ln.product == att.product]
            if not lines:
                lines.append(att)
            elif same and any(ln.qty != att.qty for ln in same):
                conflicts.append(f"the email body says {same[0].qty} x {att.product} but the attachment {filename} says {att.qty}")
    by_product: dict[str, set[int]] = {}
    for ln in lines:
        if ln.product:
            by_product.setdefault(ln.product, set()).add(ln.qty)
    for product, qtys in by_product.items():
        if len(qtys) > 1 and not any("attachment" in ln.source for ln in lines if ln.product == product):
            conflicts.append(f"two different quantities for {product}: {sorted(qtys)}")

    destination = next((c for c in sorted(cities) if re.search(rf"\b{re.escape(c)}\b", new, re.I)), None)
    deliver_by, note, ambiguous = _deadline(new, today)

    text = new
    if COMPLAINT.search(text) and not ACCEPT.search(text):
        intent = "complaint"
    elif DECLINE.search(text):
        intent = "decline"
    elif ACCEPT.search(text) and (ref or msg.in_reply_to):
        intent = "acceptance"
    elif CHANGE.search(text) and (ref or msg.in_reply_to):
        intent = "change_request"
    elif lines or ENQUIRY_WORDS.search(text):
        intent = "enquiry"
    elif "?" in text:
        intent = "question"
    else:
        intent = "other"
    if intent == "enquiry" and not lines and not unknowns:
        unknowns.append("no product and quantity could be read from the email")
    return Interpretation(intent, tuple(lines), destination, deliver_by, note, ambiguous, _validity(new, today, validity_cutoff),
                          ref, _requests(new), tuple(conflicts), tuple(unknowns))
