from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import asdict, dataclass, field

STOP_FIXED_N = "fixed-n"
STOP_SEQUENTIAL = "sequential"
STOPPING_RULES = (STOP_FIXED_N, STOP_SEQUENTIAL)

_FIELD_SEP = "\x1f"
_FINGERPRINT_DOMAIN = "commontrace-prereg-v1"


class PreregError(ValueError):
    ...


@dataclass(frozen=True)
class Preregistration:
    primary_outcome: str
    minimum_practical_effect: float
    holdout_rate: float
    planned_occasions: int
    stopping_rule: str = STOP_SEQUENTIAL
    salt: str = ""
    registered_at: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not str(self.primary_outcome).strip():
            raise PreregError(
                "a primary outcome is required: an experiment that has not named the "
                "thing it measures cannot be said to have found it"
            )
        if not 0.0 < self.minimum_practical_effect < 1.0:
            raise PreregError(
                "minimum_practical_effect must be a proportion in (0, 1), got "
                f"{self.minimum_practical_effect!r}"
            )
        if not 0.0 < self.holdout_rate < 1.0:
            raise PreregError(
                f"holdout_rate must be in (0, 1), got {self.holdout_rate!r} -- at 0 "
                "nothing is withheld and there is no control arm; at 1 nothing is "
                "injected and there is no treatment"
            )
        if self.planned_occasions <= 0:
            raise PreregError(
                f"planned_occasions must be positive, got {self.planned_occasions!r}"
            )
        if self.stopping_rule not in STOPPING_RULES:
            raise PreregError(
                f"unknown stopping rule {self.stopping_rule!r}; expected one of "
                f"{', '.join(STOPPING_RULES)}"
            )

    def fingerprint(self) -> str:
        """A stable digest of every commitment."""
        row = _FIELD_SEP.join((
            _FINGERPRINT_DOMAIN,
            self.primary_outcome,
            f"{self.minimum_practical_effect:.6f}",
            f"{self.holdout_rate:.6f}",
            str(self.planned_occasions),
            self.stopping_rule,
            self.salt,
            self.registered_at,
        ))
        return hashlib.sha256(row.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        out = asdict(self)
        out["fingerprint"] = self.fingerprint()
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> Preregistration:
        if not isinstance(raw, dict):
            raise PreregError(f"expected an object, got {type(raw).__name__}")
        known = {f for f in cls.__dataclass_fields__}
        try:
            return cls(**{k: v for k, v in raw.items() if k in known})
        except TypeError as exc:
            raise PreregError(f"cannot read pre-registration: {exc}") from exc


def register(
    primary_outcome: str,
    minimum_practical_effect: float,
    holdout_rate: float,
    planned_occasions: int,
    stopping_rule: str = STOP_SEQUENTIAL,
    salt: str = "",
    notes: str = "",
    now: datetime.datetime | None = None,
) -> Preregistration:
    """Stamp a registration at `now` (UTC)."""
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    return Preregistration(
        primary_outcome=str(primary_outcome).strip(),
        minimum_practical_effect=float(minimum_practical_effect),
        holdout_rate=float(holdout_rate),
        planned_occasions=int(planned_occasions),
        stopping_rule=stopping_rule,
        salt=str(salt),
        registered_at=moment.isoformat(),
        notes=str(notes),
    )


@dataclass(frozen=True)
class Deviation:
    """One difference between what was promised and what was done."""

    field: str
    promised: str
    actual: str
    detail: str

    @property
    def headline(self) -> str:
        return f"{self.field}: registered {self.promised}, ran {self.actual}"


@dataclass(frozen=True)
class PreregCheck:
    registered: bool
    deviations: list[Deviation] = field(default_factory=list)
    fingerprint: str = ""
    note: str = ""

    @property
    def clean(self) -> bool:
        return self.registered and not self.deviations


def check(
    prereg: Preregistration | None,
    *,
    actual_salt: str = "",
    actual_holdout_rate: float | None = None,
    actual_detectable: float | None = None,
    actual_occasions: int | None = None,
    actual_primary_outcome: str = "",
    first_observation_at: datetime.datetime | None = None,
) -> PreregCheck:
    """Diff the run that happened against the run that was promised."""
    if prereg is None:
        return PreregCheck(
            registered=False,
            note=(
                "This experiment was not pre-registered, so there is nothing to check "
                "the report against: the outcome measured, the effect size treated as "
                "meaningful, and the point at which the run stopped were all settled "
                "with the results already visible. That does not make the number "
                "wrong; it makes it unverifiable as a pre-planned test."
            ),
        )

    found: list[Deviation] = []

    if actual_salt and prereg.salt and actual_salt != prereg.salt:
        found.append(Deviation(
            field="randomization",
            promised=prereg.salt, actual=actual_salt,
            detail=(
                "The salt names the randomization. A different salt is a DIFFERENT "
                "EXPERIMENT -- every arm assignment was reshuffled -- so this "
                "registration does not describe the run being reported."
            ),
        ))

    if actual_holdout_rate is not None and abs(
        actual_holdout_rate - prereg.holdout_rate
    ) > 1e-9:
        found.append(Deviation(
            field="holdout_rate",
            promised=f"{prereg.holdout_rate:.1%}", actual=f"{actual_holdout_rate:.1%}",
            detail=(
                "The withheld fraction sets both the control arm's size and the cost "
                "of running the experiment. Changing it mid-run mixes two designs."
            ),
        ))

    if actual_detectable is not None and abs(
        actual_detectable - prereg.minimum_practical_effect
    ) > 1e-9:
        found.append(Deviation(
            field="minimum_practical_effect",
            promised=f"{prereg.minimum_practical_effect:.1%}",
            actual=f"{actual_detectable:.1%}",
            detail=(
                "This is what 'found nothing' means. Lowering it after the fact turns "
                "an underpowered null into a reportable result; raising it turns an "
                "inconvenient finding into noise."
            ),
        ))

    if actual_primary_outcome and actual_primary_outcome != prereg.primary_outcome:
        found.append(Deviation(
            field="primary_outcome",
            promised=prereg.primary_outcome, actual=actual_primary_outcome,
            detail=(
                "The endpoint moved. Every number computed on the new outcome can be "
                "individually correct and the conclusion still unsupported, because "
                "the one that was chosen is the one that looked best."
            ),
        ))

    if (
        prereg.stopping_rule == STOP_FIXED_N
        and actual_occasions is not None
        and actual_occasions < prereg.planned_occasions
    ):
        found.append(Deviation(
            field="stopping_rule",
            promised=f"{prereg.planned_occasions:,} occasions (fixed-n)",
            actual=f"{actual_occasions:,} occasions",
            detail=(
                "A fixed-n design analysed early is the classic optional stop: the "
                "estimate wanders, and stopping when it looks good selects the wander. "
                "Either run to the planned size or register the sequential rule, whose "
                "boundary accounts for looking."
            ),
        ))

    if first_observation_at is not None and prereg.registered_at:
        try:
            registered = datetime.datetime.fromisoformat(prereg.registered_at)
        except ValueError:
            registered = None
        if registered is not None:
            if registered.tzinfo is None:
                registered = registered.replace(tzinfo=datetime.timezone.utc)
            first = first_observation_at
            if first.tzinfo is None:
                first = first.replace(tzinfo=datetime.timezone.utc)
            if registered > first:
                found.append(Deviation(
                    field="registered_at",
                    promised="before the first observation",
                    actual=f"{(registered - first)} after it",
                    detail=(
                        "The registration was written once data was already arriving, "
                        "so it cannot establish that these commitments predate the "
                        "results. Usually this means the experiment started before "
                        "anyone settled what it was measuring, which is worth knowing "
                        "either way."
                    ),
                ))

    return PreregCheck(
        registered=True,
        deviations=found,
        fingerprint=prereg.fingerprint(),
        note=(
            "Pre-registered and run as described."
            if not found else
            f"Pre-registered, with {len(found)} deviation(s) from the registered "
            "design. The results may still be correct; they are no longer a test of "
            "a hypothesis fixed in advance, and should be read as exploratory."
        ),
    )


def dumps(prereg: Preregistration) -> str:
    return json.dumps(prereg.to_dict(), sort_keys=True, ensure_ascii=False, indent=2)


def loads(text: str) -> Preregistration:
    try:
        return Preregistration.from_dict(json.loads(text))
    except json.JSONDecodeError as exc:
        raise PreregError(f"pre-registration is not valid JSON: {exc}") from exc
