"""
Femm: event-driven federated model management.

    femm.federation  federation specification, deltas, impact, consistency
    femm.messages    CloudEvents-style envelope for events and commands
    femm.bus         unintelligent bus (in-memory, with reordering and duplication)
    femm.msa         Model Synchronization Agent (one per artefact)
    femm.services    Impact Analysis, Synchronization Planning, Validation, Version
    femm.runtime     in-memory wiring of a federation
    femm.satellite   the satellite federation of the MoM 2026 challenge
"""

from .federation import (AUTOMATIC, MANUAL, SEMI_AUTOMATIC, ArtefactSpec, Constraint, Federation,
                         FederationError, Rule)
from .runtime import System

__all__ = ["AUTOMATIC", "MANUAL", "SEMI_AUTOMATIC", "ArtefactSpec", "Constraint", "Federation",
           "FederationError", "Rule", "System"]
