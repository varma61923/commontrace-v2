"""Billing on proven value, and only on proven value."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass, field

from commontrace import proof

SCHEMA = "commontrace.invoice.v1"
BOOK_NAME = "billing.json"

_FIELDS = {
    "currency": str, "platform_fee_per_agent_month": (int, float), "value_share": (int, float),
    "value_basis": str, "cap_per_invoice": (int, float, type(None)), "minimum_invoice": (int, float),
    "credit_negative_value_against_platform_fee": bool, "require_signature": bool, "bill_interim": bool,
}
VALUE_BASES = ("point_estimate", "ci_low")


class PricingError(ValueError):
    """The schedule or the book cannot be used as given."""


@dataclass(frozen=True)
class PriceSchedule:
    """The commercial terms. Every field is required: there are no default prices."""

    currency: str
    platform_fee_per_agent_month: float
    value_share: float
    value_basis: str
    cap_per_invoice: float | None
    minimum_invoice: float
    credit_negative_value_against_platform_fee: bool
    require_signature: bool
    bill_interim: bool

    @classmethod
    def from_dict(cls, data: object) -> PriceSchedule:
        if not isinstance(data, dict):
            raise PricingError("a price schedule is a JSON object")
        missing = [k for k in _FIELDS if k not in data]
        extra = [k for k in data if k not in _FIELDS]
        if missing or extra:
            raise PricingError(
                "a price schedule sets every term and no others"
                + (f"; missing: {', '.join(missing)}" if missing else "")
                + (f"; unknown: {', '.join(extra)}" if extra else ""))
        for key, types in _FIELDS.items():
            value = data[key]
            if isinstance(value, bool) and types is not bool:
                raise PricingError(f"{key} must not be a boolean")
            if not isinstance(value, types):
                raise PricingError(f"{key} has the wrong type")
        if not 0 <= data["value_share"] <= 1:
            raise PricingError("value_share is a fraction between 0 and 1")
        if data["platform_fee_per_agent_month"] < 0 or data["minimum_invoice"] < 0:
            raise PricingError("fees cannot be negative")
        if data["cap_per_invoice"] is not None and data["cap_per_invoice"] < 0:
            raise PricingError("cap_per_invoice cannot be negative")
        if data["value_basis"] not in VALUE_BASES:
            raise PricingError(f"value_basis must be one of {', '.join(VALUE_BASES)}")
        if not data["currency"].strip():
            raise PricingError("currency is required")
        return cls(**data)

    @classmethod
    def from_file(cls, path: str) -> PriceSchedule:
        try:
            with open(path, encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh))
        except (OSError, ValueError) as exc:
            if isinstance(exc, PricingError):
                raise
            raise PricingError(f"cannot read the price schedule {path}: {exc}") from None


@dataclass
class Line:
    kind: str
    description: str
    amount: float
    quantity: float | None = None
    unit_price: float | None = None
    evidence: dict | None = None


@dataclass
class Invoice:
    schema: str
    org: str
    period: str
    currency: str
    lines: list = field(default_factory=list)
    refused: list = field(default_factory=list)
    value_subtotal: float = 0.0
    platform_subtotal: float = 0.0
    total: float = 0.0
    notes: list = field(default_factory=list)
    carried_credit: float = 0.0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _blank_book() -> dict:
    return {"schema": "commontrace.billing-book.v1", "experiments": {}, "invoices": []}


def read_book(directory: str) -> dict:
    path = os.path.join(directory, BOOK_NAME)
    if not os.path.isfile(path):
        return _blank_book()
    try:
        with open(path, encoding="utf-8") as fh:
            book = json.load(fh)
    except (OSError, ValueError) as exc:
        raise PricingError(f"the billing book {path} is unreadable: {exc}") from None
    if not isinstance(book, dict) or book.get("schema") != "commontrace.billing-book.v1":
        raise PricingError(f"{path} is not a billing book")
    return book


def write_book(directory: str, book: dict) -> None:
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, BOOK_NAME + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(book, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, os.path.join(directory, BOOK_NAME))


@dataclass
class Assessment:
    ok: bool
    reason: str = ""
    experiment: str = ""
    basis: float = 0.0
    evidence: dict = field(default_factory=dict)


def assess(directory: str, schedule: PriceSchedule, key: bytes | None = None) -> Assessment:
    """What this package supports billing, or why nothing."""
    try:
        checks = proof.verify(directory, key=key)
        with open(os.path.join(directory, proof.RECORD_NAME), encoding="utf-8") as fh:
            record = json.load(fh)
    except (proof.ProofError, OSError, ValueError) as exc:
        return Assessment(False, f"the package cannot be read: {exc}")
    evidence = {"label": record.get("label"), "ledger_root": record.get("ledger_root"),
                "data_digest": (record.get("evidence") or {}).get("digest"),
                "audited_at": record.get("audited_at"), "verify": f"commontrace proof verify {directory}"}
    if not proof.verified(checks):
        failed = [c.name for c in checks if c.status == proof.FAIL]
        return Assessment(False, f"the package does not verify ({', '.join(failed)}): nothing is billed on it",
                          evidence=evidence)
    if record.get("synthetic"):
        return Assessment(False, "synthetic demo data is never billed", evidence=evidence)
    if schedule.require_signature and not record.get("signature"):
        return Assessment(False, "the package is not signed and the schedule requires a signature",
                          evidence=evidence)
    if schedule.require_signature and key is None:
        return Assessment(False, "the schedule requires a signature and no key was given to check it",
                          evidence=evidence)
    if record["integrity"]["verdict"] == "COMPROMISED":
        return Assessment(False, "the experiment is COMPROMISED: no effect is stated, so none is billed",
                          evidence=evidence)
    if not record.get("final") and not schedule.bill_interim:
        return Assessment(False, "the run is interim and the schedule bills final results only", evidence=evidence)
    value = record.get("value") or {}
    if not value.get("readable") or not value.get("aggregate_readable") or value.get("money") is None:
        return Assessment(False, f"no established value: {value.get('reason') or 'the value is not readable'}",
                          evidence=evidence)
    rate = value.get("value_per_occasion")
    if not rate:
        return Assessment(False, "the proof carries no agreed worth per occasion", evidence=evidence)
    low, high = value["ci_95"]
    basis = value["money"] if schedule.value_basis == "point_estimate" else low * rate
    experiment = str((record.get("preregistration") or {}).get("salt") or record.get("preregistration_fingerprint"))
    return Assessment(True, experiment=experiment, basis=float(basis), evidence={
        **evidence, "occasions_improved": value["occasions_improved"], "ci_95": [low, high],
        "value_per_occasion": rate, "basis": schedule.value_basis})


def _round(x: float) -> float:
    return round(x + 0.0, 2)


def invoice(book: dict, schedule: PriceSchedule, *, org: str, period: str, agent_months: float,
            packages: list[str], key: bytes | None = None, commit: bool = True) -> Invoice:
    if agent_months < 0:
        raise PricingError("agent_months cannot be negative")
    if any(i["org"] == org and i["period"] == period for i in book["invoices"]):
        raise PricingError(f"{org} already has an invoice for {period}; a period is billed once")
    inv = Invoice(SCHEMA, org, period, schedule.currency)
    platform = _round(agent_months * schedule.platform_fee_per_agent_month)
    inv.platform_subtotal = platform
    if agent_months:
        inv.lines.append(Line("platform", f"Platform: {agent_months:g} agent-months", platform, agent_months,
                              schedule.platform_fee_per_agent_month))

    new_state: dict[str, dict] = {}
    value_total = 0.0
    credit_pool = float(book.get("credit", 0.0))
    for directory in packages:
        a = assess(directory, schedule, key)
        if not a.ok:
            inv.refused.append({"package": directory, "reason": a.reason, "evidence": a.evidence})
            continue
        state = book["experiments"].get(a.experiment, {"billed_basis": 0.0, "ledger_roots": []})
        if a.evidence["ledger_root"] in state["ledger_roots"]:
            inv.refused.append({"package": directory, "reason": "this package was already billed",
                                "evidence": a.evidence})
            continue
        delta = a.basis - state["billed_basis"]
        amount = _round(delta * schedule.value_share)
        evidence = {**a.evidence, "experiment": a.experiment, "basis_value": _round(a.basis),
                    "previously_charged_basis": _round(state["billed_basis"]), "share": schedule.value_share}
        if amount >= 0:
            inv.lines.append(Line("value", f"Proven value since last invoice: {a.evidence['label']}", amount,
                                  evidence=evidence))
            value_total += amount
        else:
            inv.lines.append(Line("credit", f"Proven value fell since last invoice: {a.evidence['label']}", amount,
                                  evidence=evidence))
            credit_pool += -amount
        new_state[a.experiment] = {"billed_basis": a.basis,
                                   "ledger_roots": [*state["ledger_roots"], a.evidence["ledger_root"]]}

    applied = min(credit_pool, value_total)
    if applied:
        inv.lines.append(Line("credit", "Credit carried from earlier decreases in proven value", -_round(applied)))
        value_total -= applied
        credit_pool -= applied
    if credit_pool and schedule.credit_negative_value_against_platform_fee and platform:
        against = min(credit_pool, platform)
        inv.lines.append(Line("credit", "Credit applied against the platform fee", -_round(against)))
        credit_pool -= against
        platform_after = platform - against
    else:
        platform_after = platform
    inv.value_subtotal = _round(value_total)
    total = platform_after + value_total
    if schedule.cap_per_invoice is not None and total > schedule.cap_per_invoice:
        inv.lines.append(Line("credit", f"Capped at {schedule.cap_per_invoice:g} per invoice",
                              -_round(total - schedule.cap_per_invoice)))
        inv.notes.append("The cap applies; the value basis above the cap is not carried forward as owed.")
        total = schedule.cap_per_invoice
    if total < schedule.minimum_invoice:
        inv.lines.append(Line("platform", "Minimum invoice", _round(schedule.minimum_invoice - total)))
        total = schedule.minimum_invoice
    inv.total = _round(total)
    inv.carried_credit = _round(credit_pool)
    if commit:
        book["experiments"].update(new_state)
        book["credit"] = credit_pool
        book["invoices"].append({"org": org, "period": period, "total": inv.total,
                                 "digest": hashlib.sha256(json.dumps(inv.to_dict(), sort_keys=True).encode()
                                                          ).hexdigest()})
    return inv


def render(inv: Invoice) -> str:
    lines = [f"Invoice {inv.org} {inv.period} ({inv.currency})", ""]
    for line in inv.lines:
        lines.append(f"  {line.kind:9s} {line.amount:>12,.2f}  {line.description}")
        if line.evidence:
            lines.append(f"            evidence: ledger root {line.evidence.get('ledger_root')}, "
                         f"digest {line.evidence.get('data_digest')}")
            lines.append(f"            check: {line.evidence.get('verify')}")
    lines.append(f"  {'TOTAL':9s} {inv.total:>12,.2f}")
    for r in inv.refused:
        lines.append(f"  not billed: {r['package']}: {r['reason']}")
    lines += [f"  note: {n}" for n in inv.notes]
    if inv.carried_credit:
        lines.append(f"  credit carried forward: {inv.carried_credit:,.2f}")
    return "\n".join(lines)
