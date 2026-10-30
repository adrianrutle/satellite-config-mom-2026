#!/usr/bin/env python3
"""
MT.1 -- Change Impact & Reconciliation

This module implements change detection, impact analysis, and propagation
for the satellite configuration model management challenge.

- ChangeDetector compares two snapshots (e.g. v1/ and v2/) element by element.
- ImpactAnalyzer traces each change through the traceability rules between
  artefacts and builds an explicit, composable impact graph.
- PropagationEngine reconciles one snapshot: it recomputes everything that is
  derived from the source artefacts and applies it according to a strategy.

Traceability rules (source -> target):
    catalogue.owl:<id>:mass          -> bom.json:<id>:mass_kg_per_unit
    comm-network.json:<node>         -> bom.json:<type>:quantity     (view -> source model)
    comm-network.json                -> comm-network.xml              (representation)
    bom.json:<id>:*                  -> bom.json:calculated_total_mass_kg
    bom.json                         -> bom.xml                       (representation)
    bom.json:<parameter>             -> report.md:<req>:status        (for requirements on <parameter>)
    requirements.csv:<req>           -> report.md:<req>:status
"""

import hashlib
from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

import artefacts as A
from artefacts import Snapshot

CATALOGUE = "catalogue.owl"
BOM = "bom.json"
BOM_XML = "bom.xml"
NETWORK = "comm-network.json"
NETWORK_XML = "comm-network.xml"
REQUIREMENTS = "requirements.csv"
REPORT = "report.md"
TOTAL = "calculated_total_mass_kg"


class PropagationStrategy(Enum):
    """Propagation strategies for change management."""
    MANUAL = "manual"                  # compute the plan only, change nothing
    SEMI_AUTOMATIC = "semi-automatic"  # apply value updates, leave structural edits for review
    AUTOMATIC = "automatic"            # apply every update


class GranularityLevel(Enum):
    """Granularity levels for change propagation."""
    ELEMENT = "element"  # artefact:element:attribute
    FEATURE = "feature"  # artefact:attribute (across elements)
    MODEL = "model"      # artefact


class ChangeKind(Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"


def node_id(artefact: str, element: str, attribute: str = "") -> str:
    return f"{artefact}:{element}:{attribute}" if attribute else f"{artefact}:{element}"


def coarsen(node: str, granularity: GranularityLevel) -> str:
    """Map an element-level node id to the requested granularity."""
    parts = node.split(":")
    if granularity == GranularityLevel.MODEL:
        return parts[0]
    if granularity == GranularityLevel.FEATURE:
        return f"{parts[0]}:{parts[-1] if len(parts) > 2 else '*'}"
    return node


@dataclass
class Change:
    """Represents a detected change in a model."""
    artefact: str
    element: str
    attribute: str
    old_value: Any
    new_value: Any
    kind: ChangeKind = ChangeKind.MODIFIED
    change_hash: str = ""

    def __post_init__(self):
        """Generate change hash for tracking."""
        change_str = f"{self.artefact}:{self.element}:{self.attribute}:{self.old_value}:{self.new_value}"
        self.change_hash = hashlib.sha256(change_str.encode()).hexdigest()[:8]

    @property
    def node(self) -> str:
        return node_id(self.artefact, self.element, self.attribute)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artefact": self.artefact, "element": self.element, "attribute": self.attribute,
            "kind": self.kind.value, "old_value": self.old_value, "new_value": self.new_value,
            "hash": self.change_hash,
        }


@dataclass
class Update:
    """One reconciliation step computed by the PropagationEngine."""
    artefact: str
    element: str
    attribute: str
    old_value: Any
    new_value: Any
    reason: str
    granularity: GranularityLevel = GranularityLevel.ELEMENT
    structural: bool = False
    applied: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artefact": self.artefact, "element": self.element, "attribute": self.attribute,
            "old_value": self.old_value, "new_value": self.new_value, "reason": self.reason,
            "granularity": self.granularity.value, "structural": self.structural, "applied": self.applied,
        }


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

class ChangeDetector:
    """Detects changes between two snapshots of the artefact set."""

    def detect(self, old: Snapshot, new: Snapshot) -> List[Change]:
        return (
            self.detect_catalogue_changes(old, new)
            + self.detect_bom_changes(old.bom, new.bom)
            + self.detect_network_changes(old.network, new.network)
            + self.detect_requirement_changes(old.requirements, new.requirements)
        )

    @staticmethod
    def _diff_keyed(artefact: str, old: Dict[str, Dict], new: Dict[str, Dict], attributes: List[str]) -> List[Change]:
        changes = []
        for key in old.keys() - new.keys():
            changes.append(Change(artefact, key, "", old[key], None, ChangeKind.REMOVED))
        for key in new.keys() - old.keys():
            changes.append(Change(artefact, key, "", None, new[key], ChangeKind.ADDED))
        for key in old.keys() & new.keys():
            for attr in attributes:
                if old[key].get(attr) != new[key].get(attr):
                    changes.append(Change(artefact, key, attr, old[key].get(attr), new[key].get(attr)))
        return sorted(changes, key=lambda c: (c.element, c.attribute))

    def detect_catalogue_changes(self, old: Snapshot, new: Snapshot) -> List[Change]:
        def as_dict(catalogue):
            return {cid: {"mass": p.mass_kg, "class": p.part_class} for cid, p in catalogue.items()}
        return self._diff_keyed(CATALOGUE, as_dict(old.catalogue), as_dict(new.catalogue), ["mass", "class"])

    def detect_bom_changes(self, old: Dict[str, Any], new: Dict[str, Any]) -> List[Change]:
        def as_dict(bom):
            # Duplicate entries for one type are merged so that diffs stay per component type.
            merged: Dict[str, Dict] = {}
            for c in bom.get("components", []):
                entry = merged.setdefault(c["type"], {"quantity": 0, "mass_kg_per_unit": c["mass_kg_per_unit"]})
                entry["quantity"] += c["quantity"]
            return merged
        changes = self._diff_keyed(BOM, as_dict(old), as_dict(new), ["quantity", "mass_kg_per_unit"])
        if old.get(TOTAL) != new.get(TOTAL):
            changes.append(Change(BOM, "total_system", TOTAL, old.get(TOTAL), new.get(TOTAL)))
        return changes

    def detect_network_changes(self, old: Dict[str, Any], new: Dict[str, Any]) -> List[Change]:
        old_nodes = {n["component_id"]: n for n in old.get("nodes", [])}
        new_nodes = {n["component_id"]: n for n in new.get("nodes", [])}
        changes = self._diff_keyed(NETWORK, old_nodes, new_nodes, ["type", "role"])

        # A removed and an added node with the same type and role is reported as a rename.
        removed = [c for c in changes if c.kind == ChangeKind.REMOVED]
        added = [c for c in changes if c.kind == ChangeKind.ADDED]
        for r in removed:
            match = next((a for a in added if (a.new_value["type"], a.new_value["role"]) ==
                          (r.old_value["type"], r.old_value["role"])), None)
            if match:
                added.remove(match)
                changes.remove(r)
                changes.remove(match)
                changes.append(Change(NETWORK, match.element, "component_id", r.element, match.element))

        def links(network):
            return {f"{l['source']}->{l['target']}": l for l in network.get("links", [])}
        changes += self._diff_keyed(NETWORK, links(old), links(new), ["type", "protocol"])
        return changes

    def detect_requirement_changes(self, old: List[Dict[str, str]], new: List[Dict[str, str]]) -> List[Change]:
        return self._diff_keyed(
            REQUIREMENTS,
            {r["RequirementID"]: r for r in old},
            {r["RequirementID"]: r for r in new},
            ["Description", "Parameter", "Operator", "Value", "Unit"],
        )


# ---------------------------------------------------------------------------
# Impact analysis
# ---------------------------------------------------------------------------

@dataclass
class ImpactGraph:
    """Directed graph of impacted elements; composable across changes."""
    nodes: Dict[str, Set[str]] = field(default_factory=dict)        # node -> hashes of changes reaching it
    edges: Set[Tuple[str, str, str]] = field(default_factory=set)   # (from, to, relation)
    roots: Dict[str, str] = field(default_factory=dict)             # change hash -> root node

    def add_edge(self, source: str, target: str, relation: str):
        self.edges.add((source, target, relation))

    def merge(self, other: "ImpactGraph") -> "ImpactGraph":
        for node, causes in other.nodes.items():
            self.nodes.setdefault(node, set()).update(causes)
        self.edges |= other.edges
        self.roots.update(other.roots)
        return self

    def successors(self, node: str) -> List[Tuple[str, str]]:
        return sorted((t, rel) for s, t, rel in self.edges if s == node)

    def paths_from(self, root: str) -> List[List[str]]:
        """All maximal propagation paths starting at root (for deep inspection)."""
        paths, stack = [], [[root]]
        while stack:
            path = stack.pop()
            nexts = [t for t, _ in self.successors(path[-1]) if t not in path]
            if not nexts:
                paths.append(path)
            stack += [path + [t] for t in nexts]
        return sorted(paths)

    def coarsened(self, granularity: GranularityLevel) -> "ImpactGraph":
        graph = ImpactGraph(roots={h: coarsen(n, granularity) for h, n in self.roots.items()})
        for node, causes in self.nodes.items():
            graph.nodes.setdefault(coarsen(node, granularity), set()).update(causes)
        for s, t, rel in self.edges:
            cs, ct = coarsen(s, granularity), coarsen(t, granularity)
            if cs != ct:
                graph.add_edge(cs, ct, rel)
        return graph

    def to_dict(self) -> Dict[str, Any]:
        return {
            "roots": self.roots,
            "nodes": {n: sorted(c) for n, c in sorted(self.nodes.items())},
            "edges": [{"from": s, "to": t, "type": rel} for s, t, rel in sorted(self.edges)],
        }


class ImpactAnalyzer:
    """Traces changes through the traceability rules of a (target) snapshot."""

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self.bom_types = {c["type"] for c in snapshot.bom.get("components", [])}
        self.requirements_by_param: Dict[str, List[str]] = {}
        for req in snapshot.requirements:
            self.requirements_by_param.setdefault(req["Parameter"], []).append(req["RequirementID"])

    def _direct_impacts(self, node: str, change: Optional[Change] = None) -> List[Tuple[str, str]]:
        parts = node.split(":")
        artefact = parts[0]
        element = parts[1] if len(parts) > 1 else ""
        attribute = parts[2] if len(parts) > 2 else ""
        impacts: List[Tuple[str, str]] = []

        if artefact == CATALOGUE and attribute in ("mass", "") and element in self.bom_types:
            impacts.append((node_id(BOM, element, "mass_kg_per_unit"), "direct_update"))

        elif artefact == NETWORK:
            impacts.append((NETWORK_XML, "representation_sync"))
            node_types = set()
            for value in (change.old_value, change.new_value) if change else ():
                if isinstance(value, dict) and "component_id" in value:
                    node_types.add(value["type"])
            if change and change.attribute == "type" and "->" not in element:
                node_types |= {change.old_value, change.new_value}
            for node_type in sorted(node_types):
                impacts.append((node_id(BOM, node_type, "quantity"), "view_to_source"))

        elif artefact == BOM and element != "total_system":
            impacts.append((node_id(BOM, "total_system", TOTAL), "derived_computation"))
            impacts.append((BOM_XML, "representation_sync"))

        elif artefact == BOM and element == "total_system":
            impacts.append((BOM_XML, "representation_sync"))
            for req_id in self.requirements_by_param.get(attribute, []):
                impacts.append((node_id(REPORT, req_id, "status"), "requirement_verification"))

        elif artefact == REQUIREMENTS:
            impacts.append((node_id(REPORT, element, "status"), "requirement_verification"))

        return impacts

    def analyze(self, changes: List[Change]) -> ImpactGraph:
        """Build one impact graph per change and compose them into a single graph."""
        graph = ImpactGraph()
        for change in changes:
            graph.merge(self.analyze_change(change))
        return graph

    def analyze_change(self, change: Change) -> ImpactGraph:
        graph = ImpactGraph(roots={change.change_hash: change.node})
        queue = deque([change.node])
        graph.nodes[change.node] = {change.change_hash}
        while queue:
            current = queue.popleft()
            for target, relation in self._direct_impacts(current, change if current == change.node else None):
                graph.add_edge(current, target, relation)
                if target not in graph.nodes:
                    graph.nodes[target] = set()
                    queue.append(target)
                graph.nodes[target].add(change.change_hash)
        return graph


# ---------------------------------------------------------------------------
# Propagation / reconciliation
# ---------------------------------------------------------------------------

@dataclass
class PropagationResult:
    strategy: PropagationStrategy
    updates: List[Update]
    bom: Dict[str, Any]
    contents: Dict[str, str]                     # relative path -> rendered text for applied artefacts
    verification: List[Dict[str, Any]]
    errors: List[str] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return not self.updates and not self.errors

    @property
    def pending(self) -> List[Update]:
        return [u for u in self.updates if not u.applied]

    @property
    def all_satisfied(self) -> bool:
        return all(v["satisfied"] for v in self.verification)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "consistent": self.consistent,
            "updates": [u.to_dict() for u in self.updates],
            "errors": self.errors,
            "verification": self.verification,
            "all_satisfied": self.all_satisfied,
        }


class PropagationEngine:
    """Reconciles a snapshot with its source artefacts according to a strategy."""

    def __init__(self, strategy: PropagationStrategy = PropagationStrategy.SEMI_AUTOMATIC):
        """Initialize with propagation strategy."""
        self.strategy = strategy

    def _performs(self, structural: bool) -> bool:
        """Whether an update is carried out in memory (MANUAL simulates everything as a preview)."""
        return not (self.strategy == PropagationStrategy.SEMI_AUTOMATIC and structural)

    def reconcile(self, snapshot: Snapshot, baseline: Optional[Snapshot] = None) -> PropagationResult:
        """
        Compute (and, depending on the strategy, apply) every update needed so that
        derived artefacts agree with the source artefacts of the snapshot.

        A baseline snapshot lets the engine notice component types that disappeared
        from the communications view entirely, so they can be removed from the BOM.
        """
        updates: List[Update] = []
        errors: List[str] = []
        bom = A.clone_bom(snapshot.bom)

        def record(update: Update) -> bool:
            performed = self._performs(update.structural)
            update.applied = performed and self.strategy != PropagationStrategy.MANUAL
            updates.append(update)
            return performed

        # 1. Communications view -> BOM structure (quantities of comms components).
        node_counts = Counter(n["type"] for n in snapshot.network.get("nodes", []))
        comms_types = set(node_counts)
        if baseline is not None:
            comms_types |= {n["type"] for n in baseline.network.get("nodes", [])}

        components: List[Dict[str, Any]] = []
        seen: Dict[str, Dict[str, Any]] = {}
        for comp in bom.get("components", []):
            if comp["type"] in seen:
                kept = seen[comp["type"]]
                if record(Update(BOM, comp["type"], "entry", "duplicate entry", "merged",
                                 "duplicate BOM entries for one component type", structural=True)):
                    kept["quantity"] += comp["quantity"]
                    continue
            else:
                seen[comp["type"]] = comp
            components.append(comp)

        for comp_type in sorted(comms_types):
            expected = node_counts.get(comp_type, 0)
            comp = seen.get(comp_type)
            if comp is None and expected:
                part = snapshot.catalogue.get(comp_type)
                if part is None or part.mass_kg is None:
                    errors.append(f"{comp_type} is used in {NETWORK} but has no mass in {CATALOGUE}")
                    continue
                new_comp = {"type": comp_type, "quantity": expected, "mass_kg_per_unit": part.mass_kg}
                if record(Update(BOM, comp_type, "entry", None, new_comp,
                                 f"{NETWORK} has {expected} node(s) of type {comp_type}", structural=True)):
                    components.append(new_comp)
                    seen[comp_type] = new_comp
            elif comp is not None and comp["quantity"] != expected:
                if expected == 0:
                    if record(Update(BOM, comp_type, "entry", comp, None,
                                     f"{comp_type} no longer used in {NETWORK}", structural=True)):
                        components.remove(comp)
                        del seen[comp_type]
                elif record(Update(BOM, comp_type, "quantity", comp["quantity"], expected,
                                   f"{NETWORK} has {expected} node(s) of type {comp_type}", structural=True)):
                    comp["quantity"] = expected
        bom["components"] = components

        # 2. Catalogue -> BOM unit masses.
        for comp in components:
            part = snapshot.catalogue.get(comp["type"])
            if part is None or part.mass_kg is None:
                errors.append(f"BOM component {comp['type']} has no mass in {CATALOGUE}")
                continue
            if comp["mass_kg_per_unit"] != part.mass_kg:
                if record(Update(BOM, comp["type"], "mass_kg_per_unit", comp["mass_kg_per_unit"], part.mass_kg,
                                 f"{CATALOGUE}:{comp['type']}:mass")):
                    comp["mass_kg_per_unit"] = part.mass_kg

        # 3. Derived total mass.
        total = A.compute_total_mass(bom)
        if bom.get(TOTAL) != total:
            if record(Update(BOM, "total_system", TOTAL, bom.get(TOTAL), total,
                             "sum of quantity x mass_kg_per_unit", GranularityLevel.FEATURE)):
                bom[TOTAL] = total

        contents: Dict[str, str] = {}
        if any(u.artefact == BOM and self._performs(u.structural) for u in updates):
            contents[A.BOM_JSON] = A.dump_bom_json(bom)

        # 4. Representations: bom.xml and comm-network.xml mirror their JSON source.
        bom_xml = A.render_bom_xml(bom)
        if snapshot.bom_xml_text is None or not A.bom_equivalent(A.parse_bom_xml(snapshot.bom_xml_text), bom):
            if record(Update(BOM_XML, "root", "", "out of sync", "regenerated from bom.json",
                             "representation of bom.json", GranularityLevel.MODEL)):
                contents[A.BOM_XML] = bom_xml

        network_xml = A.render_network_xml(snapshot.network)
        if snapshot.network_xml_text is None or A.parse_network_xml(snapshot.network_xml_text) != snapshot.network:
            if record(Update(NETWORK_XML, "root", "", "out of sync", "regenerated from comm-network.json",
                             "representation of comm-network.json", GranularityLevel.MODEL)):
                contents[A.NETWORK_XML] = network_xml

        # 5. Requirements x BOM -> report.
        verification = A.evaluate_requirements(snapshot.requirements, bom)
        for v in verification:
            if v["satisfied"] is None:
                errors.append(f"{v['requirement']}: parameter {v['parameter']} not found in {BOM}")
        report = A.render_report(snapshot.requirements, bom)
        if snapshot.report_text != report:
            if record(Update(REPORT, "root", "", "out of date", "regenerated",
                             "requirements.csv evaluated against bom.json", GranularityLevel.MODEL)):
                contents[A.REPORT] = report

        return PropagationResult(self.strategy, updates, bom, contents, verification, errors)

    def apply(self, snapshot: Snapshot, result: PropagationResult) -> List[str]:
        """Write the applied artefacts to disk; returns the files that changed."""
        if self.strategy == PropagationStrategy.MANUAL:
            return []
        return A.write_artefacts(snapshot.root, result.contents)
