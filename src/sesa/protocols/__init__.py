"""Protocol registry.

Five protocols ship with sesa, all of the same shape: phases run in sequence, the moves
inside a phase run in parallel.

Of these, ``reflect`` is not a way of deliberating but a **control baseline for what
debate achieves**: everyone sees only their own last round and nobody sees anybody.
Without it, the sentence "the debate changed X% of the participants' positions" does
not hold — how much of that X% would have changed anyway is unknown.
Third parties can inject their own with :func:`register`, without changing this package.
"""

from __future__ import annotations

from ..i18n import t
from .adversarial import AdversarialProtocol
from .base import Move, Phase, Protocol
from .council import CouncilProtocol
from .debate import DebateProtocol
from .ensemble import EnsembleProtocol
from .reflect import ReflectProtocol

_REGISTRY: dict[str, type[Protocol]] = {
    EnsembleProtocol.name: EnsembleProtocol,
    DebateProtocol.name: DebateProtocol,
    CouncilProtocol.name: CouncilProtocol,
    AdversarialProtocol.name: AdversarialProtocol,
    ReflectProtocol.name: ReflectProtocol,
}


def register(protocol_cls: type[Protocol]) -> type[Protocol]:
    if not protocol_cls.name:
        raise ValueError(t("a protocol must define a non-empty name"))
    _REGISTRY[protocol_cls.name] = protocol_cls
    return protocol_cls


def available() -> list[str]:
    return sorted(_REGISTRY)


#: Default value of each option. A setting equal to the default does not deserve an "ignored"
#: warning — the user expressed no intent.
_DEFAULTS = {"turn_taking": "parallel", "proposer": "rotate"}

#: Which option means anything to which protocols. The caller (the CLI) hands every key in the
#: config to every protocol indiscriminately, and the ones that do not know a key **must not warn**
#: — that is not the user misconfiguring anything, it is the caller taking a shortcut. Warning
#: anyway is a wall of false alarms, and false alarms train the user to ignore warnings.
_ONLY_FOR = {"proposer": {"adversarial"}, "turn_taking": {"debate", "council"}}

#: Options a protocol **knows and deliberately overrides**. That is a different thing from "does not
#: recognise": council's all-see-all semantics require everyone to speak from the same snapshot, so
#: parallel is forced. Reporting it as "does not recognise these options, ignored" makes the user
#: think they made a typo and go check a configuration that is perfectly correct.
_OVERRIDDEN = {
    ("council", "turn_taking"): lambda: t(
        "council's everyone-sees-everyone semantics require all turns in parallel"
    )
}


def build(name: str, **options) -> Protocol:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            t(
                "unknown protocol {name}; available: {options}",
                name=repr(name),
                options=", ".join(available()),
            )
        )
    # Pass in only the parameters the protocol knows, so one extra key in the config does not blow
    # the whole thing up
    import inspect
    import warnings

    accepted = inspect.signature(cls.__init__).parameters
    named = {k for k, p in accepted.items() if p.kind is not inspect.Parameter.VAR_KEYWORD}
    takes_kwargs = len(named) < len(accepted)
    # The base class's `__init__(**options)` accepts everything and drops it into self.options,
    # which nobody reads — **that** is the real silent-discard point, not the filtering below. Warn
    # only about what the **user explicitly changed**. The CLI hands turn_taking / proposer to every
    # protocol indiscriminately, and warning about all of them would flood a perfectly normal
    # configuration — worse than the silent discard, and it trains the user to ignore warnings.
    dropped = sorted(
        k
        for k, v in options.items()
        if k not in named
        and k != "self"
        and v not in (None, "", _DEFAULTS.get(k))
        and name in _ONLY_FOR.get(k, {name})
    )
    if dropped:
        # The silent discard is deliberate (one extra key in the config must not kill the run), but
        # it **cannot be completely silent**: a user who writes `turn_taking: sequential` for a
        # protocol that does not know it will believe the setting took effect, while the behaviour
        # is entirely the default.
        # The wording has to distinguish two cases. For an option the protocol **knows and
        # deliberately overrides** (council forcing parallel), saying "does not recognise these
        # options" sends the user to check a configuration with no typo in it — the warning itself
        # is right (their setting really did not take effect); what is wrong is that it states the
        # reason backwards.
        overridden = [k for k in dropped if (name, k) in _OVERRIDDEN]
        unknown = [k for k in dropped if (name, k) not in _OVERRIDDEN]
        parts = []
        if unknown:
            parts.append(
                t(
                    "protocol {name} does not recognise these options and ignored them: {keys}",
                    name=name,
                    keys=t("\u3001").join(unknown),
                )
            )
        for key in overridden:
            parts.append(
                t(
                    "protocol {name} overrides {key} ({why}), so the value you configured "
                    "has no effect",
                    name=name,
                    key=key,
                    why=_OVERRIDDEN[(name, key)](),
                )
            )
        warnings.warn(t("\uff1b").join(parts), RuntimeWarning, stacklevel=2)
    if takes_kwargs:
        return cls(**options)
    return cls(**{k: v for k, v in options.items() if k in named})


__all__ = [
    "AdversarialProtocol",
    "CouncilProtocol",
    "DebateProtocol",
    "EnsembleProtocol",
    "Move",
    "Phase",
    "Protocol",
    "ReflectProtocol",
    "available",
    "build",
    "register",
]
