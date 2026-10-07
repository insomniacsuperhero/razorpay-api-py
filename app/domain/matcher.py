"""Deterministic validation: AI facts are checked against the TRUSTED Razorpay payment, then the
rule table turns field + checks into a verdict. Comparisons are conservative: only clear
contradictions count as mismatches; a missing fact is never a mismatch."""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from app.domain.evidence_rules import FIELD_LABELS, KEY_FACTS, Rule

STRONG, RELEVANT, NEEDS_ATTENTION, NOT_USEFUL = "strong", "relevant", "needs_attention", "not_useful"
USEFUL = {STRONG, RELEVANT}


@dataclass
class PaymentContext:
    payment_id: str
    amount: int                       # paise
    disputed_amount: int
    created_at: datetime
    contact_last4: str | None = None
    order_refs: set[str] = field(default_factory=set)   # order_id, receipt, notes.order_id, normalised

    @classmethod
    def from_razorpay(cls, payment: dict, dispute: dict, order: dict | None = None) -> "PaymentContext":
        notes = payment.get("notes") or {}
        notes = notes if isinstance(notes, dict) else {}
        refs = {payment.get("order_id"), notes.get("order_id"), notes.get("receipt"),
                (order or {}).get("receipt"), (order or {}).get("id")}
        digits = re.sub(r"\D", "", payment.get("contact") or "")
        return cls(payment_id=payment["id"], amount=int(payment.get("amount") or dispute.get("amount") or 0),
                   disputed_amount=int(dispute.get("amount") or payment.get("amount") or 0),
                   created_at=datetime.fromtimestamp(int(payment.get("created_at") or 0), tz=timezone.utc),
                   contact_last4=digits[-4:] if len(digits) >= 4 else None,
                   order_refs={_norm(r) for r in refs if r})


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def _parse_date(s) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def rupees(paise: int) -> str:
    r = paise / 100
    return f"₹{r:,.0f}" if r == int(r) else f"₹{r:,.2f}"


def check_facts(facts: dict, ctx: PaymentContext) -> tuple[list[dict], list[str]]:
    """Returns (mismatches, matched fact keys)."""
    mismatches, matched = [], []
    amt = facts.get("amount_paise")
    if amt is not None:
        if int(amt) in (ctx.amount, ctx.disputed_amount):
            matched.append("amount_paise")
        else:
            mismatches.append({"fact": "amount_paise", "found": rupees(int(amt)), "expected": rupees(ctx.amount),
                               "message": f"Shows {rupees(int(amt))}, but the payment was {rupees(ctx.amount)}."})
    paid_on = ctx.created_at.date()
    for key, label in (("delivery_date", "Delivery date"), ("refund_date", "Refund date")):
        d = _parse_date(facts.get(key))
        if d is None:
            continue
        if d < paid_on - timedelta(days=1):
            mismatches.append({"fact": key, "found": d.isoformat(), "expected": f"on or after {paid_on.isoformat()}",
                               "message": f"{label} {d:%d %b %Y} is before the payment on {paid_on:%d %b %Y}."})
        elif d <= paid_on + timedelta(days=60):
            matched.append(key)
    last4 = facts.get("contact_last4")
    if last4 and ctx.contact_last4:
        if re.sub(r"\D", "", str(last4))[-4:] == ctx.contact_last4:
            matched.append("contact_last4")
        else:
            mismatches.append({"fact": "contact_last4", "found": f"…{last4}", "expected": f"…{ctx.contact_last4}",
                               "message": "Phone number doesn't match the customer on this payment."})
    oid = facts.get("order_id")
    if oid and ctx.order_refs:
        if _norm(oid) in ctx.order_refs:
            matched.append("order_id")
        else:
            mismatches.append({"fact": "order_id", "found": oid, "expected": ", ".join(sorted(ctx.order_refs)),
                               "message": f"Order {oid} doesn't match this payment's order."})
    return mismatches, matched


@dataclass
class Evaluation:
    verdict: str
    reason: str
    mismatches: list[dict]
    matched: list[str]


def evaluate(fld: str | None, facts: dict, readable: bool, quality_issues: list[str],
             rule: Rule, ctx: PaymentContext) -> Evaluation:
    if not readable:
        issue = quality_issues[0] if quality_issues else "Key details can't be read."
        return Evaluation(NEEDS_ATTENTION, f"{issue} Add a clearer photo or screenshot.", [], [])
    role = rule.role(fld)
    if role == "none":
        label = FIELD_LABELS.get(fld or "", "This file")
        return Evaluation(NOT_USEFUL, f"{label} doesn't address '{rule.reason_label.lower()}'.", [], [])
    mismatches, matched = check_facts(facts, ctx)
    if mismatches:
        return Evaluation(NEEDS_ATTENTION, mismatches[0]["message"], mismatches, matched)
    key = KEY_FACTS.get(fld, ())
    has_key = not key or any(facts.get(k) for k in key)
    if role == "required" and has_key:
        why = {"shipping_proof": "Delivery details are readable",
               "customer_communication": "The customer's own words are readable",
               "billing_proof": "Invoice details are readable",
               "refund_confirmation": "Refund reference is readable",
               "proof_of_service": "The photo is clear"}.get(fld, "Key details are readable")
        tail = " and match the payment." if matched else "."
        return Evaluation(STRONG, why + tail, [], matched)
    if role == "required":
        return Evaluation(RELEVANT, f"Fits {FIELD_LABELS[fld].lower()}, but the key detail isn't visible.", [], matched)
    return Evaluation(RELEVANT, f"Supports the response as {FIELD_LABELS[fld].lower()}.", [], matched)
