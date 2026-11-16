"""
Model Synchronization Agent (Section 3.3, Figure 2).

One MSA owns one artefact and is the only component that writes it
(Proposition 1, Authority). It

  * detects local edits, computes the delta and publishes a change event;
  * receives commands addressed to its artefact, lets the owner's execution
    policy (or the owner) approve or reject them, and applies an approved
    command only if its base version is still the current version
    (Proposition 4, No lost updates); otherwise it reports a conflict.

Each MSA handles one message at a time (A2) and ignores duplicates (A1).
Commands that arrive while another one waits for approval are queued.
"""

import copy
from collections import deque
from typing import Any, Deque, List, Optional, Set, Tuple

from . import messages as M
from .bus import Bus
from .federation import AUTOMATIC, MANUAL, SEMI_AUTOMATIC, ArtefactSpec, canonical
from .messages import Message

CONSISTENT = "Consistent"
MODIFIED = "Modified"
PENDING = "Pending approval"
APPLYING = "Applying"
CONFLICT = "Conflict"


class Store:
    """Technology adapter: reads and writes the artefact in its native format."""

    def read(self) -> Any:
        raise NotImplementedError

    def write(self, state: Any) -> None:
        raise NotImplementedError


class MemoryStore(Store):
    def __init__(self, state: Any):
        self.state = copy.deepcopy(state)

    def read(self) -> Any:
        return copy.deepcopy(self.state)

    def write(self, state: Any) -> None:
        self.state = copy.deepcopy(state)


class MSA:
    def __init__(self, spec: ArtefactSpec, store: Store, bus: Bus, version: int = 1,
                 check_base_version: bool = True):
        self.spec = spec
        self.name = spec.name
        self.store = store
        self.bus = bus
        self.version = version
        self.check_base_version = check_base_version     # switched off only to show what it prevents
        self.phase = CONSISTENT
        self.pending: Optional[Message] = None
        self.queue: Deque[Message] = deque()
        self.seen: Set[str] = set()
        self.known = store.read()                         # last state published
        self.transitions: List[Tuple[str, str, str]] = [] # (from, to, label): lifecycle trace
        self.lost = 0                                     # ghost counter: writes with an outdated base
        self.rejected = 0
        bus.subscribe(M.command_topic(self.name), self.on_command)

    @property
    def policy(self) -> str:
        return self.spec.policy

    @property
    def state(self) -> Any:
        return self.store.read()

    def _goto(self, phase: str, label: str) -> None:
        self.transitions.append((self.phase, phase, label))
        self.phase = phase

    def _publish(self, message: Message) -> Message:
        self.bus.publish(M.EVENTS_TOPIC, message)
        return message

    # -- local edits ------------------------------------------------------------

    def local_edit(self, state: Any, base_version: Optional[int] = None,
                   author: Optional[str] = None) -> Optional[Message]:
        """
        A team member saves a new state of the artefact. An edit made against an
        older version (base_version) is not written but reported as a conflict.
        """
        if base_version is not None and base_version != self.version:
            return self._publish(Message(
                M.CONFLICT, self.name, artefact=self.name, version=self.version, base_version=base_version,
                data={"state": self.state, "proposed": state, "author": author,
                      "deltas": self.spec.deltas(self.state, state), "reason": "local edit on outdated version"}))
        self.store.write(state)
        return self.detect(author)

    def detect(self, author: Optional[str] = None) -> Optional[Message]:
        """Compare the stored artefact with the last published state (e.g. after a file was saved)."""
        current = self.store.read()
        deltas = self.spec.deltas(self.known, current)
        if not deltas:
            return None
        previous = self.phase
        self._goto(MODIFIED, "local edit detected")
        event = self._changed(current, deltas, None, None, author=author)
        self._goto(previous, "published delta events")
        return event

    def _changed(self, state: Any, deltas: List[dict], correlation: Optional[str],
                 causation: Optional[str], author: Optional[str] = None) -> Message:
        self.version += 1
        self.known = copy.deepcopy(state)
        return self._publish(Message(
            M.CHANGED, self.name, artefact=self.name, subject=deltas[0]["ref"] if deltas else None,
            base_version=self.version - 1, version=self.version,
            correlation_id=correlation, causation_id=causation,
            data={"deltas": deltas, "state": state, "author": author}))

    # -- commands -----------------------------------------------------------------

    def on_command(self, command: Message) -> None:
        if command.id in self.seen:          # A1: duplicate delivery
            return
        self.seen.add(command.id)
        if self.phase == PENDING:
            self.queue.append(command)
            return
        self._receive(command)

    def _receive(self, command: Message) -> None:
        self.pending = command
        self._goto(PENDING, "command received")
        if self.check_base_version and command.base_version != self.version:
            # Already outdated on arrival: report the conflict without asking the owner.
            self._conflict(command, self.store.read())
            self._next()
            return
        structural = bool(command.data.get("structural"))
        self._publish(Message(
            M.RECEIVED, self.name, artefact=self.name, base_version=command.base_version,
            correlation_id=command.correlation_id, causation_id=command.id,
            data={"structural": structural, "policy": self.policy, "deltas": command.data.get("deltas", [])}))
        if self.policy == AUTOMATIC or (self.policy == SEMI_AUTOMATIC and not structural):
            self.approve(command.id, by=f"policy:{self.policy}")

    def awaiting(self) -> Optional[Message]:
        """The command waiting for the owner's decision, if any."""
        return self.pending if self.phase == PENDING else None

    def approve(self, command_id: Optional[str] = None, by: str = "owner") -> Optional[Message]:
        command = self._take(command_id)
        self._goto(APPLYING, f"approved by {by}")
        current = self.store.read()
        if command.base_version != self.version and self.check_base_version:
            result = self._conflict(command, current)
        else:
            if command.base_version != self.version:
                self.lost += 1
            new = command.data["state"]
            self.store.write(new)
            self._goto(MODIFIED, "applied")
            result = self._changed(new, self.spec.deltas(current, new), command.correlation_id, command.id,
                                   author=by)
            self._goto(CONSISTENT, "published delta events")
        self._next()
        return result

    def _conflict(self, command: Message, current: Any) -> Message:
        self.pending = None
        self._goto(CONFLICT, "base version mismatch")
        result = self._publish(Message(
            M.CONFLICT, self.name, artefact=self.name, version=self.version, base_version=command.base_version,
            correlation_id=command.correlation_id, causation_id=command.id,
            data={"state": current, "proposed": command.data["state"], "reason": "outdated base version"}))
        self._goto(CONSISTENT, "conflict published")
        return result

    def reject(self, command_id: Optional[str] = None, reason: str = "", by: str = "owner") -> Message:
        command = self._take(command_id)
        self.rejected += 1
        result = self._publish(Message(
            M.REJECTED, self.name, artefact=self.name, base_version=command.base_version,
            correlation_id=command.correlation_id, causation_id=command.id,
            data={"reason": reason, "by": by}))
        self._goto(CONSISTENT, "rejected")
        self._next()
        return result

    def _take(self, command_id: Optional[str]) -> Message:
        if self.phase != PENDING or self.pending is None:
            raise RuntimeError(f"{self.name}: no command waits for approval")
        if command_id is not None and command_id != self.pending.id:
            raise RuntimeError(f"{self.name}: command {command_id} is not the pending command {self.pending.id}")
        command, self.pending = self.pending, None
        return command

    def _next(self) -> None:
        if self.queue and self.phase != PENDING:
            self._receive(self.queue.popleft())

    def consistent_with_store(self) -> bool:
        return canonical(self.known) == canonical(self.store.read())
