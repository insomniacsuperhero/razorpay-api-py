"""Rule-based coverage: required items present + matching -> Good coverage. No scores."""
from app.domain.evidence_rules import FIELD_LABELS, Rule
from app.domain.matcher import STRONG, USEFUL


def compute_coverage(rule: Rule, items: list) -> dict:
    """items: active EvidenceItem rows (not removed, status ready)."""
    by_field: dict[str, list] = {}
    for it in items:
        if it.verdict in USEFUL and it.final_field and it.razorpay_document_id:
            by_field.setdefault(it.final_field, []).append(it)

    def entry(fld, role):
        its = by_field.get(fld, [])
        state = "strong" if any(i.verdict == STRONG for i in its) else ("present" if its else "missing")
        return {"field": fld, "label": FIELD_LABELS.get(fld, fld), "role": role, "state": state,
                "evidence_ids": [str(i.id) for i in its]}

    required = [entry(f, "required") for f in rule.required]
    helpful = [entry(f, "helpful") for f in rule.helpful]
    complete = all(r["state"] != "missing" for r in required) and bool(by_field)
    good = complete and all(r["state"] == "strong" for r in required)
    needs_attention = [str(i.id) for i in items if i.verdict == "needs_attention"]
    return {"required": required, "helpful": helpful, "complete": complete,
            "label": "Good coverage" if good else ("Enough to submit" if complete else "Missing required proof"),
            "needs_attention": needs_attention,
            "fields_with_documents": {f: [i.razorpay_document_id for i in its] for f, its in by_field.items()}}
