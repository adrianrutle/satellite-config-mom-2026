"""
Message envelope shared by events and commands.

The envelope follows the CloudEvents attribute names (id, type, source, subject,
time, data) and adds the protocol attributes of the paper:

    artefact       the artefact the message is about
    base_version   version the change was computed against (commands, conflicts)
    version        version the artefact has after the change (events)
    correlation_id identifier of the plan the message belongs to (A4)
    causation_id   identifier of the message that caused this one

Messages are plain data: the bus serialises them to JSON, so no object is ever
shared between components.
"""

import itertools
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

# Event types
CHANGED = "femm.artefact.changed"              # an artefact has a new version (local edit or command)
CONFLICT = "femm.conflict.detected"            # a command or edit was based on an outdated version
REJECTED = "femm.command.rejected"             # the owner rejected a command
RECEIVED = "femm.command.received"             # a command waits for the owner's approval
IMPACT = "femm.impact.computed"                # Impact Analysis result for one change event
PLAN_STARTED = "femm.plan.started"
PLAN_COMPLETED = "femm.plan.completed"
EVALUATED = "femm.requirement.evaluated"       # Validation result changed
BASELINE = "femm.baseline.created"

# Command types
UPDATE = "femm.command.update"                 # replace the artefact by a proposed new state

EVENTS_TOPIC = "events"


def command_topic(artefact: str) -> str:
    """Commands are addressed to the owner of one artefact via its own topic."""
    return f"commands.{artefact}"


_counter = itertools.count(1)


def new_id(prefix: str = "m") -> str:
    """Deterministic identifiers make runs reproducible for a given schedule."""
    return f"{prefix}{next(_counter)}"


def reset_ids() -> None:
    global _counter
    _counter = itertools.count(1)


@dataclass
class Message:
    type: str
    source: str
    artefact: Optional[str] = None
    subject: Optional[str] = None
    version: Optional[int] = None
    base_version: Optional[int] = None
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    time: int = 0

    @property
    def is_command(self) -> bool:
        return self.type.startswith("femm.command.") and self.type not in (REJECTED, RECEIVED)

    def to_json(self) -> str:
        return json.dumps(asdict(self))   # key order is part of a state (rendering)

    @staticmethod
    def from_json(text: str) -> "Message":
        return Message(**json.loads(text))

    def short(self) -> str:
        """One-line rendering for traces."""
        kind = self.type.rsplit(".", 2)
        name = ".".join(kind[-2:])
        parts = [f"{self.id:>5} {name:<20} {self.source:<22}"]
        if self.artefact:
            ver = f" v{self.base_version}->v{self.version}" if self.version is not None and self.base_version is not None \
                else (f" v{self.version}" if self.version is not None else
                      (f" base v{self.base_version}" if self.base_version is not None else ""))
            parts.append(f"{self.artefact}{ver}")
        if self.correlation_id:
            parts.append(f"plan={self.correlation_id}")
        if self.causation_id:
            parts.append(f"cause={self.causation_id}")
        return "  ".join(parts)
