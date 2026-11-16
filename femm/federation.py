"""
Federation specification (Definition 1 of the paper): F = (A, own, E, R).

    A    artefacts, each with an owning team and that team's execution policy
    own  one Model Synchronization Agent per artefact (created by the runtime)
    E    element references, written  artefact:element:feature
    R    correspondence rules; each has source artefacts, one target artefact
         and a function computing the new target state from the sources

A rule function receives a *view* (artefact -> state) and the current state of
its target, and returns the new target state. Passing the current target lets a
rule preserve the parts of the target that it does not own (for example the
locally maintained components of a BoM). Several rules may target the same
artefact; they are applied in declaration order. A rule whose target is also
one of its sources is a *derivation* inside an artefact (e.g. the total mass of
the BoM); it must be idempotent.

States are JSON-compatible values (dicts, lists, strings, numbers).
"""

import copy
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

AUTOMATIC = "automatic"
SEMI_AUTOMATIC = "semi-automatic"
MANUAL = "manual"
POLICIES = (AUTOMATIC, SEMI_AUTOMATIC, MANUAL)

CORRESPONDENCE = "correspondence"
DERIVATION = "derivation"
REPRESENTATION = "representation"

View = Dict[str, Any]
Delta = Dict[str, Any]          # {"ref": "a:e:f", "kind": "added|removed|modified", "old": ..., "new": ...}


# ---------------------------------------------------------------------------
# Deltas
# ---------------------------------------------------------------------------

KEY_FIELDS = ("id", "component_id", "type", "RequirementID", "name")


def _list_key(*lists: List[Any]) -> Optional[str]:
    """A field that identifies every item within each of the lists of dicts, if there is one."""
    if not any(lists) or not all(isinstance(i, dict) for items in lists for i in items):
        return None
    for key in KEY_FIELDS:
        def identifies(items):
            values = [i.get(key) for i in items]
            return all(v is not None for v in values) and len(set(map(str, values))) == len(values)
        if all(identifies(items) for items in lists):
            return key
    return None


def json_diff(artefact: str, old: Any, new: Any, path: Tuple[str, ...] = ()) -> List[Delta]:
    """Element-level differences between two JSON-compatible states."""
    ref = ":".join((artefact,) + path) if path else f"{artefact}:root"
    if old == new:
        return []
    if isinstance(old, dict) and isinstance(new, dict):
        deltas: List[Delta] = []
        for key in sorted(set(old) | set(new), key=str):
            if key not in old:
                deltas.append({"ref": ":".join((artefact,) + path + (str(key),)), "kind": "added", "old": None, "new": new[key]})
            elif key not in new:
                deltas.append({"ref": ":".join((artefact,) + path + (str(key),)), "kind": "removed", "old": old[key], "new": None})
            else:
                deltas += json_diff(artefact, old[key], new[key], path + (str(key),))
        return deltas
    if isinstance(old, list) and isinstance(new, list):
        key = _list_key(old, new)
        if key is not None:
            return json_diff(artefact, {str(i[key]): i for i in old}, {str(i[key]): i for i in new}, path)
    return [{"ref": ref, "kind": "modified", "old": old, "new": new}]


def is_structural(deltas: Iterable[Delta]) -> bool:
    """Structural changes add or remove elements; value updates only modify them."""
    return any(d["kind"] in ("added", "removed") for d in deltas)


def ref_artefact(ref: str) -> str:
    return ref.split(":", 1)[0]


# ---------------------------------------------------------------------------
# Specification
# ---------------------------------------------------------------------------

@dataclass
class ArtefactSpec:
    name: str
    owner: str                                   # owning team
    policy: str = AUTOMATIC                      # execution policy of the owner for commands
    diff: Optional[Callable[[Any, Any], List[Delta]]] = None   # delta engine; default: json_diff

    def deltas(self, old: Any, new: Any) -> List[Delta]:
        return self.diff(old, new) if self.diff else json_diff(self.name, old, new)


@dataclass
class Rule:
    name: str
    kind: str
    sources: Tuple[str, ...]
    target: str
    fn: Callable[[View, Any], Any]
    # Element-level impact: target references affected by a source reference. For the
    # changed references themselves the delta is passed as well; the view is the
    # Impact Analysis service's current knowledge of the federation (may be empty).
    # Default: the target artefact as a whole.
    impacts: Optional[Callable[[str, Optional[Delta], View], List[str]]] = None

    def impacted(self, ref: str, delta: Optional[Delta] = None, view: Optional[View] = None) -> List[str]:
        if ref_artefact(ref) not in self.sources:
            return []
        if self.impacts is None:
            return [] if ref_artefact(ref) == self.target else [self.target]
        return self.impacts(ref, delta, view or {})

    @property
    def internal(self) -> bool:
        return self.target in self.sources


@dataclass
class Constraint:
    """A validation constraint (e.g. REQ001); returns one result per requirement id."""
    name: str
    sources: Tuple[str, ...]
    evaluate: Callable[[View], Dict[str, Optional[bool]]]


@dataclass
class ImpactGraph:
    """Impact of a set of deltas: reachable references with typed edges (Section 3.5)."""
    roots: List[str] = field(default_factory=list)
    nodes: Set[str] = field(default_factory=set)
    edges: Set[Tuple[str, str, str]] = field(default_factory=set)   # (from, to, "kind:rule")

    @property
    def artefacts(self) -> Set[str]:
        return {ref_artefact(n) for n in self.nodes}

    def coarsened(self) -> "ImpactGraph":
        """Artefact-level graph."""
        graph = ImpactGraph(sorted({ref_artefact(r) for r in self.roots}), set(self.artefacts))
        graph.edges = {(ref_artefact(s), ref_artefact(t), k) for s, t, k in self.edges if ref_artefact(s) != ref_artefact(t)}
        return graph

    def to_dict(self) -> Dict[str, Any]:
        return {"roots": sorted(self.roots), "nodes": sorted(self.nodes),
                "edges": [{"from": s, "to": t, "rule": k} for s, t, k in sorted(self.edges)]}

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "ImpactGraph":
        return ImpactGraph(list(data["roots"]), set(data["nodes"]),
                           {(e["from"], e["to"], e["rule"]) for e in data["edges"]})


class FederationError(Exception):
    pass


class Federation:
    def __init__(self, artefacts: Sequence[ArtefactSpec], rules: Sequence[Rule],
                 constraints: Sequence[Constraint] = ()):
        self.artefacts: Dict[str, ArtefactSpec] = {a.name: a for a in artefacts}
        self.rules: List[Rule] = list(rules)
        self.constraints: List[Constraint] = list(constraints)
        self.check()
        self.order = self._topological_order()

    # -- well-formedness ---------------------------------------------------

    def check(self) -> None:
        for a in self.artefacts.values():
            if a.policy not in POLICIES:
                raise FederationError(f"{a.name}: unknown policy {a.policy!r}")
        for r in self.rules:
            for name in r.sources + (r.target,):
                if name not in self.artefacts:
                    raise FederationError(f"rule {r.name}: unknown artefact {name!r}")
        for c in self.constraints:
            for name in c.sources:
                if name not in self.artefacts:
                    raise FederationError(f"constraint {c.name}: unknown artefact {name!r}")

    def _topological_order(self) -> List[str]:
        """Order of artefacts along the rule graph; fails if it has a cycle (A3)."""
        succ: Dict[str, Set[str]] = {a: set() for a in self.artefacts}
        indeg = {a: 0 for a in self.artefacts}
        for r in self.rules:
            for s in r.sources:
                if s != r.target and r.target not in succ[s]:
                    succ[s].add(r.target)
                    indeg[r.target] += 1
        queue = deque(a for a in self.artefacts if indeg[a] == 0)
        order = []
        while queue:
            a = queue.popleft()
            order.append(a)
            for t in sorted(succ[a]):
                indeg[t] -= 1
                if indeg[t] == 0:
                    queue.append(t)
        if len(order) != len(self.artefacts):
            cyclic = sorted(set(self.artefacts) - set(order))
            raise FederationError(f"rule graph is cyclic (assumption A3) among {cyclic}")
        return order

    # -- rule graph ----------------------------------------------------------

    def rules_into(self, target: str) -> List[Rule]:
        return [r for r in self.rules if r.target == target]

    def rules_from(self, source: str) -> List[Rule]:
        return [r for r in self.rules if source in r.sources]

    def downstream(self, artefact: str) -> List[str]:
        """Direct targets of rules with the artefact as a source (incl. itself for derivations)."""
        targets = {r.target for r in self.rules_from(artefact)}
        return [a for a in self.order if a in targets]

    def derived(self) -> List[str]:
        return [a for a in self.order if self.rules_into(a)]

    # -- semantics ------------------------------------------------------------

    def compute(self, target: str, view: View, current: Any = None) -> Any:
        """New state of `target` given the view; `current` defaults to view[target]."""
        local = copy.deepcopy(view)
        if current is not None:
            local[target] = copy.deepcopy(current)
        for rule in self.rules_into(target):
            local[target] = rule.fn(local, local[target])
        return local[target]

    def violations(self, view: View) -> List[str]:
        """Targets whose rules do not hold in the view (Definition 2)."""
        return [t for t in self.derived() if canonical(self.compute(t, view)) != canonical(view[t])]

    def consistent(self, view: View) -> bool:
        return not self.violations(view)

    def deltas(self, artefact: str, old: Any, new: Any) -> List[Delta]:
        return self.artefacts[artefact].deltas(old, new)

    def impact(self, deltas: List[Delta], view: Optional[View] = None) -> ImpactGraph:
        """Subgraph of the rule graph reachable from the changed references."""
        graph = ImpactGraph(roots=[d["ref"] for d in deltas])
        queue = deque((d["ref"], d) for d in deltas)
        graph.nodes.update(graph.roots)
        while queue:
            ref, delta = queue.popleft()
            for rule in self.rules_from(ref_artefact(ref)):
                for target in rule.impacted(ref, delta, view):
                    graph.edges.add((ref, target, f"{rule.kind}:{rule.name}"))
                    if target not in graph.nodes:
                        graph.nodes.add(target)
                        queue.append((target, None))
        return graph

    def evaluate(self, view: View) -> Dict[str, Optional[bool]]:
        results: Dict[str, Optional[bool]] = {}
        for c in self.constraints:
            results.update(c.evaluate(view))
        return results


def canonical(value: Any) -> str:
    """Canonical form used to compare states (key order and int/float independent)."""
    def norm(v):
        if isinstance(v, bool) or v is None or isinstance(v, str):
            return v
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            return {str(k): norm(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        return v
    return json.dumps(norm(value), sort_keys=True)
