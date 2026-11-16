"""
Wiring of one federation: an MSA per artefact plus the services, on one bus.

This is the in-memory realisation used for tests, scenarios and fault
injection. Other realisations (Restate, Git) host the same MSA and service
logic on their own transport and storage.
"""

from typing import Any, Dict, List, Optional

from . import messages as M
from .bus import Bus, InMemoryBus
from .federation import Federation, View
from .messages import Message
from .msa import MSA, MemoryStore, Store
from .services import ImpactAnalysis, SynchronizationPlanning, Validation, Version


class System:
    def __init__(self, federation: Federation, initial: View, bus: Optional[Bus] = None,
                 stores: Optional[Dict[str, Store]] = None, check_base_version: bool = True):
        self.federation = federation
        self.bus = bus if bus is not None else InMemoryBus()
        stores = stores or {}
        self.msas: Dict[str, MSA] = {
            name: MSA(spec, stores.get(name) or MemoryStore(initial[name]), self.bus,
                      check_base_version=check_base_version)
            for name, spec in federation.artefacts.items()
        }
        versions = {name: msa.version for name, msa in self.msas.items()}
        view = self.view()
        self.impact = ImpactAnalysis(self.bus, federation, versions, view)
        self.planner = SynchronizationPlanning(self.bus, federation, versions, view)
        self.validation = Validation(self.bus, federation, versions, view)
        self.versions = Version(self.bus, federation, versions, view)

    # -- state ------------------------------------------------------------------

    def view(self) -> View:
        return {name: msa.state for name, msa in self.msas.items()}

    def consistent(self) -> bool:
        return self.federation.consistent(self.view())

    def violations(self) -> List[str]:
        return self.federation.violations(self.view())

    def awaiting(self) -> Dict[str, Message]:
        """Commands waiting for an owner's decision."""
        return {name: msa.awaiting() for name, msa in self.msas.items() if msa.awaiting() is not None}

    @property
    def quiescent(self) -> bool:
        return self.bus.idle and not self.awaiting() and not any(m.queue for m in self.msas.values())

    @property
    def lost_updates(self) -> int:
        return sum(m.lost for m in self.msas.values())

    @property
    def rejections(self) -> int:
        return sum(m.rejected for m in self.msas.values())

    # -- driving ------------------------------------------------------------------

    def edit(self, artefact: str, state: Any, base_version: Optional[int] = None,
             author: Optional[str] = None) -> Optional[Message]:
        return self.msas[artefact].local_edit(state, base_version, author)

    def approve(self, artefact: str, by: str = "owner") -> Optional[Message]:
        return self.msas[artefact].approve(by=by)

    def reject(self, artefact: str, reason: str = "", by: str = "owner") -> Message:
        return self.msas[artefact].reject(reason=reason, by=by)

    def run(self, max_steps: int = 100_000) -> int:
        return self.bus.run(max_steps) if isinstance(self.bus, InMemoryBus) else 0

    def settle(self, approve: bool = True, max_rounds: int = 1000) -> None:
        """Run, approving (or rejecting) every waiting command, until nothing is left."""
        for _ in range(max_rounds):
            self.run()
            waiting = self.awaiting()
            if not waiting:
                return
            for name in waiting:
                self.approve(name) if approve else self.reject(name, "rejected by settle()")
        raise RuntimeError("did not settle")

    # -- inspection -------------------------------------------------------------------

    def trace(self, types: Optional[List[str]] = None) -> List[str]:
        if not isinstance(self.bus, InMemoryBus):
            return []
        return [f"{topic:<26} {m.short()}" for topic, m in self.bus.log if types is None or m.type in types]

    def messages(self, type_: Optional[str] = None) -> List[Message]:
        if not isinstance(self.bus, InMemoryBus):
            return []
        return [m for _, m in self.bus.log if type_ is None or m.type == type_]
