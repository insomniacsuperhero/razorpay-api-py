"""Deterministic first-draft summary built ONLY from facts on the evidence cards (Guide §11.1).
No second LLM call, so nothing can be invented. Max 1,000 characters (Razorpay limit)."""
from datetime import date

from app.domain.matcher import USEFUL, PaymentContext, rupees

MAX = 1000


def _d(s) -> str | None:
    try:
        return date.fromisoformat(str(s)[:10]).strftime("%-d %b %Y")
    except (ValueError, TypeError):
        return None


def build_summary(ctx: PaymentContext, reason_label: str, items: list) -> str:
    useful = [i for i in items if i.verdict in USEFUL and not i.removed_at]
    f: dict = {}
    for it in sorted(useful, key=lambda i: i.verdict != "strong"):
        for k, v in (it.final_facts or {}).items():
            f.setdefault(k, v)

    order = f" for order {f['order_id']}" if f.get("order_id") else ""
    parts = [f"Payment {ctx.payment_id} of {rupees(ctx.amount)}{order} was made on {ctx.created_at:%-d %b %Y}."]
    if f.get("delivery_status") or f.get("tracking_id"):
        s = "The order was delivered" if (f.get("delivery_status") or "").lower().startswith("deliver") else \
            f"Courier status: {f.get('delivery_status') or 'shipped'}"
        if _d(f.get("delivery_date")):
            s += f" on {_d(f['delivery_date'])}"
        if f.get("courier"):
            s += f" by {f['courier']}"
        if f.get("tracking_id"):
            s += f", tracking ID {f['tracking_id']}"
        parts.append(s + ".")
    if f.get("customer_quote"):
        when = f" on {_d(f['message_date'])}" if _d(f.get("message_date")) else ""
        parts.append(f"The customer wrote{when}: \"{f['customer_quote'][:200]}\".")
    if f.get("invoice_number"):
        amt = f" for {rupees(int(f['amount_paise']))}" if f.get("amount_paise") else ""
        # only claim an attached invoice when a billing document is actually in the pack
        has_invoice_doc = any(i.final_field == "billing_proof" for i in useful if getattr(i, "final_field", None))
        parts.append(f"Invoice {f['invoice_number']}{amt} is attached." if has_invoice_doc
                     else f"The evidence references invoice {f['invoice_number']}{amt}.")
    if f.get("refund_reference"):
        when = f" on {_d(f['refund_date'])}" if _d(f.get("refund_date")) else ""
        parts.append(f"A refund was processed{when}, reference {f['refund_reference']}.")
    labels = sorted({(i.document_type or "").strip() for i in useful} - {""})
    if labels:
        parts.append("Evidence attached: " + "; ".join(l[0].lower() + l[1:] for l in labels) + ".")
    text = " ".join(parts)
    return text if len(text) <= MAX else text[: MAX - 1].rsplit(" ", 1)[0] + "…"
