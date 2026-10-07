"""Fixed evidence rules (UI/UX Spec v2 §6, Backend Guide §7). Code — not the model — decides
what each dispute reason requires. Reason codes are the mock's readable demo labels."""
from dataclasses import dataclass

ALL_FIELDS = ["shipping_proof", "billing_proof", "cancellation_proof", "customer_communication",
              "proof_of_service", "explanation_letter", "refund_confirmation", "access_activity_log",
              "refund_cancellation_policy", "term_and_conditions", "others"]

FIELD_LABELS = {
    "shipping_proof": "Delivery confirmation",
    "billing_proof": "Invoice or bill",
    "cancellation_proof": "Cancellation record",
    "customer_communication": "Customer communication",
    "proof_of_service": "Product or service proof",
    "explanation_letter": "Explanation letter",
    "refund_confirmation": "Refund confirmation",
    "access_activity_log": "Account activity",
    "refund_cancellation_policy": "Refund and cancellation policy",
    "term_and_conditions": "Terms and conditions",
    "others": "Other proof",
}


@dataclass(frozen=True)
class Rule:
    reason_label: str
    required: tuple[str, ...]
    helpful: tuple[str, ...]
    hint: str

    def role(self, fld: str | None) -> str:
        if fld in self.required:
            return "required"
        if fld in self.helpful:
            return "helpful"
        return "none"


DISPUTE_RULES: dict[str, Rule] = {
    "goods_not_received": Rule("Customer says the order never arrived", ("shipping_proof",),
                               ("customer_communication", "billing_proof"),
                               "Courier tracking or a delivery screenshot helps most."),
    "not_as_described": Rule("Customer says the item was damaged or different", ("proof_of_service",),
                             ("customer_communication", "refund_cancellation_policy", "shipping_proof"),
                             "Packing or product photos and your chat about the item help most."),
    "unauthorized": Rule("Customer says they didn't make this payment", ("customer_communication",),
                         ("access_activity_log", "shipping_proof", "billing_proof"),
                         "A chat with the customer placing the order helps most."),
    "duplicate": Rule("Customer says they were charged twice", ("billing_proof",), ("others",),
                      "Invoices showing two separate orders help most."),
    "refund_not_processed": Rule("Customer says a promised refund didn't arrive", ("refund_confirmation",),
                                 ("cancellation_proof", "customer_communication"),
                                 "A refund screenshot with its reference number helps most."),
}

# Facts that must be present for a required field to count as "Strong"
KEY_FACTS: dict[str, tuple[str, ...]] = {
    "shipping_proof": ("tracking_id", "delivery_date", "delivery_status"),
    "customer_communication": ("customer_quote",),
    "billing_proof": ("invoice_number", "amount_paise", "order_id"),
    "refund_confirmation": ("refund_reference", "refund_date"),
    "cancellation_proof": ("order_id", "message_date", "customer_quote"),
    "proof_of_service": (),          # a readable photo is itself the proof
    "others": (),
}


def rule_for(reason_code: str) -> Rule:
    # Unknown (e.g. a real network code not yet mapped): nothing is forced, everything counts as helpful
    return DISPUTE_RULES.get(reason_code) or Rule(reason_code.replace("_", " ").capitalize(), (), tuple(ALL_FIELDS), "")
