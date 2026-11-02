"""
Model Management Services (Section 3.5, Table 3).

Every service owns one explicit model and communicates only through the bus:

    Megamodel                  the federation specification (femm.federation.Federation)
    Impact Analysis            impact graphs, composable per plan
    Synchronization Planning   plans; the only service that sends commands
    Validation                 constraint results
    Version                    event log, versions and baselines

Each service ignores messages it has already processed (A1).
"""

import copy
import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from . import messages as M
from .bus import Bus
from .federation import Federation, ImpactGraph, canonical, is_structural
from .messages import Message


class Service:
    name = "service"
    deduplicate = True        # A1; switched off in tests to show robustness without it

    def __init__(self, bus: Bus):
        self.bus = bus
        self.seen: Set[str] = set()
        bus.subscribe(M.EVENTS_TOPIC, self._deliver)

    def _deliver(self, message: Message) -> None:
        if self.deduplicate and message.id in self.seen:
            return
        self.seen.add(message.id)
        self.handle(message)

    def handle(self, message: Message) -> None:
        raise NotImplementedError

    def publish(self, message: Message, topic: str = M.EVENTS_TOPIC) -> Message:
        self.bus.publish(topic, message)
        return message


# ---------------------------------------------------------------------------
# Impact Analysis
# ---------------------------------------------------------------------------

class ImpactAnalysis(Service):
    """For every change event, the subgraph of the rule graph reachable from its deltas."""
    name = "impact-analysis"

    def __init__(self, bus: Bus, federation: Federation, versions: Dict[str, int], view: Dict[str, Any]):
        super().__init__(bus)
        self.federation = federation
        self.versions = dict(versions)
        self.view = copy.deepcopy(view)
        self.graphs: Dict[str, ImpactGraph] = {}       # event id -> graph
        self.by_plan: Dict[str, ImpactGraph] = {}      # plan id -> merged graph

    def handle(self, message: Message) -> None:
        if message.type == M.CHANGED:
            if message.version > self.versions.get(message.artefact, 0):
                self.versions[message.artefact] = message.version
                self.view[message.artefact] = message.data["state"]
            graph = self.federation.impact(message.data["deltas"], self.view)
            self.graphs[message.id] = graph
            if message.correlation_id:
                self.plan_graph(message.correlation_id, graph)
            self.publish(Message(
                M.IMPACT, self.name, artefact=message.artefact, version=message.version,
                correlation_id=message.correlation_id, causation_id=message.id,
                data={"graph": graph.to_dict(), "event": {
                    "id": message.id, "artefact": message.artefact, "version": message.version,
                    "state": message.data["state"], "correlation_id": message.correlation_id,
                    "causation_id": message.causation_id}}))
        elif message.type == M.PLAN_STARTED:
            origin = self.graphs.get(message.causation_id or "")
            if origin is not None:
                self.plan_graph(message.correlation_id, origin)

    def plan_graph(self, plan: str, graph: ImpactGraph) -> ImpactGraph:
        merged = self.by_plan.setdefault(plan, ImpactGraph())
        merged.roots = sorted(set(merged.roots) | set(graph.roots))
        merged.nodes |= graph.nodes
        merged.edges |= graph.edges
        return merged


# ---------------------------------------------------------------------------
# Synchronization Planning
# ---------------------------------------------------------------------------

@dataclass
class Step:
    command: str
    target: str
    base_version: int
    structural: bool
    status: str = "open"            # open | applied | conflict | rejected | superseded
    cause: Optional[str] = None


@dataclass
class Plan:
    id: str
    origin: str                      # id of the change event that started the plan
    artefact: str
    steps: List[Step] = field(default_factory=list)
    completed: bool = False

    @property
    def open(self) -> List[Step]:
        return [s for s in self.steps if s.status == "open"]

    @property
    def status(self) -> str:
        return "rejected" if any(s.status == "rejected" for s in self.steps) else "completed"


class SynchronizationPlanning(Service):
    """
    Turns change events (via their impact) into commands, one per affected target.

    The planner keeps a view of the federation: version and state per artefact.
    The view is advanced only by change events, which are also the only triggers
    of downstream planning. A conflict causes the affected step to be recomputed
    against the current state of its target, without touching the view. (The
    model checker found that advancing the view on conflicts loses updates when
    a conflict message is duplicated; see maude/ in the paper repository.)
    """
    name = "synchronization-planning"

    def __init__(self, bus: Bus, federation: Federation, versions: Dict[str, int], view: Dict[str, Any]):
        super().__init__(bus)
        self.federation = federation
        self.known: Dict[str, Tuple[int, Any]] = {a: (versions[a], copy.deepcopy(view[a])) for a in view}
        self.plans: Dict[str, Plan] = {}
        self.steps: Dict[str, Tuple[str, Step]] = {}      # command id -> (plan id, step)
        self._plan_ids = itertools.count(1)

    def view(self) -> Dict[str, Any]:
        return {a: s for a, (_, s) in self.known.items()}

    def handle(self, message: Message) -> None:
        if message.type == M.IMPACT:
            self._on_change(message)
        elif message.type == M.CONFLICT and message.causation_id in self.steps:
            self._on_conflict(message)
        elif message.type == M.REJECTED and message.causation_id in self.steps:
            plan, step = self.steps[message.causation_id]
            step.status = "rejected"
            self._maybe_complete(plan)

    def _on_change(self, impact: Message) -> None:
        event = impact.data["event"]
        artefact, version, plan = event["artefact"], event["version"], event["correlation_id"]
        if event["causation_id"] in self.steps:                  # confirmation of a plan step (A4)
            plan, step = self.steps[event["causation_id"]]
            if step.status == "open":
                step.status = "applied"
        if version <= self.known[artefact][0]:                   # stale or duplicate event
            if plan:
                self._maybe_complete(plan)
            return
        self.known[artefact] = (version, copy.deepcopy(event["state"]))
        if plan is None:                                         # a local edit starts a new plan
            plan = f"plan{next(self._plan_ids)}"
            self.plans[plan] = Plan(plan, event["id"], artefact)
            self.publish(Message(M.PLAN_STARTED, self.name, artefact=artefact, correlation_id=plan,
                                 causation_id=event["id"], data={"origin": event["id"]}))
        affected = ImpactGraph.from_dict(impact.data["graph"]).artefacts
        targets = self.federation.downstream(artefact)
        if self.federation.rules_into(artefact) and artefact not in targets:
            targets.insert(0, artefact)      # a local edit of a derived artefact: re-check its own rules
        for target in targets:
            if target in affected:
                version_t, state_t = self.known[target]
                self._send(plan, target, state_t, version_t, cause=event["id"])
        self._maybe_complete(plan)

    def _on_conflict(self, conflict: Message) -> None:
        plan, step = self.steps[conflict.causation_id]
        if step.status != "open":
            return
        step.status = "conflict"
        self._send(plan, step.target, conflict.data["state"], conflict.version, cause=conflict.id)
        self._maybe_complete(plan)

    def _send(self, plan: str, target: str, current: Any, base_version: int, cause: str) -> Optional[Message]:
        new = self.federation.compute(target, self.view(), current)
        if canonical(new) == canonical(current):
            return None
        deltas = self.federation.deltas(target, current, new)
        command = Message(M.UPDATE, self.name, artefact=target, base_version=base_version,
                          subject=deltas[0]["ref"] if deltas else None,
                          correlation_id=plan, causation_id=cause,
                          data={"state": new, "deltas": deltas, "structural": is_structural(deltas)})
        step = Step(command.id, target, base_version, is_structural(deltas), cause=cause)
        self.plans[plan].steps.append(step)
        self.steps[command.id] = (plan, step)
        return self.publish(command, M.command_topic(target))

    def _maybe_complete(self, plan: str) -> None:
        p = self.plans.get(plan)
        if p is None or p.completed or p.open:
            return
        p.completed = True
        self.publish(Message(M.PLAN_COMPLETED, self.name, artefact=p.artefact, correlation_id=plan,
                             causation_id=p.origin,
                             data={"status": p.status,
                                   "steps": [{"command": s.command, "target": s.target, "status": s.status,
                                              "structural": s.structural} for s in p.steps]}))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class Validation(Service):
    """Evaluates the constraints of the federation when the values they depend on change."""
    name = "validation"

    def __init__(self, bus: Bus, federation: Federation, versions: Dict[str, int], view: Dict[str, Any]):
        super().__init__(bus)
        self.federation = federation
        self.versions = dict(versions)
        self.view = copy.deepcopy(view)
        self.results: Dict[str, Optional[bool]] = federation.evaluate(self.view)

    def handle(self, message: Message) -> None:
        if message.type != M.CHANGED or message.version <= self.versions.get(message.artefact, 0):
            return
        self.versions[message.artefact] = message.version
        self.view[message.artefact] = message.data["state"]
        if not any(message.artefact in c.sources for c in self.federation.constraints):
            return
        results = self.federation.evaluate(self.view)
        for requirement in sorted(set(results) | set(self.results)):
            if results.get(requirement) != self.results.get(requirement):
                self.publish(Message(M.EVALUATED, self.name, subject=requirement,
                                     correlation_id=message.correlation_id, causation_id=message.id,
                                     data={"requirement": requirement, "satisfied": results.get(requirement),
                                           "previous": self.results.get(requirement)}))
        self.results = results


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

class Version(Service):
    """Keeps the event log and records a baseline whenever the federation is consistent and no plan is open."""
    name = "version"

    def __init__(self, bus: Bus, federation: Federation, versions: Dict[str, int], view: Dict[str, Any]):
        super().__init__(bus)
        self.federation = federation
        self.versions = dict(versions)
        self.view = copy.deepcopy(view)
        self.log: List[Dict[str, Any]] = []
        self.open_plans: Set[str] = set()
        self.closed_plans: Set[str] = set()
        self.baselines: List[Dict[str, Any]] = []
        if federation.consistent(self.view):
            self.baselines.append({"versions": dict(self.versions), "plan": None})

    def handle(self, message: Message) -> None:
        self.log.append({"id": message.id, "type": message.type, "source": message.source,
                         "artefact": message.artefact, "version": message.version,
                         "correlation_id": message.correlation_id, "causation_id": message.causation_id})
        if message.type == M.CHANGED and message.version > self.versions.get(message.artefact, 0):
            self.versions[message.artefact] = message.version
            self.view[message.artefact] = message.data["state"]
        elif message.type == M.PLAN_STARTED and message.correlation_id not in self.closed_plans:
            self.open_plans.add(message.correlation_id)
        elif message.type == M.PLAN_COMPLETED:
            self.open_plans.discard(message.correlation_id)
            self.closed_plans.add(message.correlation_id)
        else:
            return
        self._maybe_baseline(message)

    def _maybe_baseline(self, message: Message) -> None:
        if self.open_plans or not self.federation.consistent(self.view):
            return
        if self.baselines and self.baselines[-1]["versions"] == self.versions:
            return
        baseline = {"versions": dict(self.versions), "plan": message.correlation_id}
        self.baselines.append(baseline)
        self.publish(Message(M.BASELINE, self.name, correlation_id=message.correlation_id,
                             causation_id=message.id, data=baseline))
