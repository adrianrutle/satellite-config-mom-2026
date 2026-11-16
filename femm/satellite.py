"""
The satellite federation of the MoM 2026 challenge, as a federation specification.

Artefacts (owner, policy):
    catalogue     Parts Catalogue, OWL              manufacturer            automatic
    network       Network Topology, JSON            communications_engineer automatic
    network_xml   XML representation of network     communications_engineer automatic
    requirements  Requirements, CSV                 requirements_engineer   automatic
    bom           Bill of Materials, JSON           systems_engineer        semi-automatic
    bom_xml       XML representation of bom         systems_engineer        automatic
    report        Verification report, Markdown     verification_engineer   automatic

Rules (Table 2 of the paper):
    network_to_bom   corr.   nodes of type t in the network       -> bom: quantity of t
    catalogue_to_bom corr.   catalogue: mass of c                  -> bom: unit mass of c
    bom_total        deriv.  bom: all components                   -> bom: total mass
    bom_to_xml       repr.   bom.json                              -> bom.xml
    network_to_xml   repr.   comm-network.json                     -> comm-network.xml
    report           corr.   requirements, bom                     -> report: status per requirement

The loaders and renderers of tools/artefacts.py are the technology adapters.
"""

import copy
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

TOOLS = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import artefacts as A                                    # noqa: E402
from mt1_change_impact import Change, ChangeDetector     # noqa: E402

from .federation import (AUTOMATIC, CORRESPONDENCE, DERIVATION, REPRESENTATION, SEMI_AUTOMATIC,  # noqa: E402
                         ArtefactSpec, Constraint, Federation, Rule, View)
from .msa import Store                                   # noqa: E402

CATALOGUE, NETWORK, NETWORK_XML = "catalogue", "network", "network_xml"
REQUIREMENTS, BOM, BOM_XML, REPORT = "requirements", "bom", "bom_xml", "report"
TOTAL = "calculated_total_mass_kg"

# Catalogue classes whose components are covered by the communications view:
# their BoM quantity is the number of network nodes of that type.
VIEW_CLASSES = ("Antenna", "CommunicationController")

PATHS = {
    CATALOGUE: A.CATALOGUE, NETWORK: A.NETWORK_JSON, NETWORK_XML: A.NETWORK_XML,
    REQUIREMENTS: A.REQUIREMENTS, BOM: A.BOM_JSON, BOM_XML: A.BOM_XML, REPORT: A.REPORT,
}


# ---------------------------------------------------------------------------
# Delta engines (reuse the MT.1 change detector)
# ---------------------------------------------------------------------------

def _deltas(artefact: str, changes: List[Change]) -> List[Dict[str, Any]]:
    return [{"ref": ":".join(p for p in (artefact, c.element, c.attribute) if p),
             "kind": c.kind.value, "old": c.old_value, "new": c.new_value} for c in changes]


def _text_diff(artefact: str):
    def diff(old: str, new: str):
        return [] if old == new else [{"ref": f"{artefact}:root", "kind": "modified", "old": None, "new": None}]
    return diff


def catalogue_diff(old: Dict[str, Any], new: Dict[str, Any]):
    return _deltas(CATALOGUE, ChangeDetector._diff_keyed("catalogue", old, new, ["mass", "class"]))


def network_diff(old: Dict[str, Any], new: Dict[str, Any]):
    return _deltas(NETWORK, ChangeDetector().detect_network_changes(old, new))


def bom_diff(old: Dict[str, Any], new: Dict[str, Any]):
    return _deltas(BOM, ChangeDetector().detect_bom_changes(old, new))


def requirements_diff(old: List[Dict[str, str]], new: List[Dict[str, str]]):
    return _deltas(REQUIREMENTS, ChangeDetector().detect_requirement_changes(old, new))


# ---------------------------------------------------------------------------
# Rule functions
# ---------------------------------------------------------------------------

def network_to_bom(view: View, bom: Dict[str, Any]) -> Dict[str, Any]:
    """Quantities of components covered by the network view; new components take the catalogue mass."""
    bom = copy.deepcopy(bom)
    catalogue = view[CATALOGUE]
    counts = Counter(n["type"] for n in view[NETWORK].get("nodes", []))
    covered = {cid for cid, part in catalogue.items() if part["class"] in VIEW_CLASSES} | set(counts)

    components: List[Dict[str, Any]] = []
    by_type: Dict[str, Dict[str, Any]] = {}
    for comp in bom.get("components", []):       # merge duplicate entries of one type
        if comp["type"] in by_type:
            by_type[comp["type"]]["quantity"] += comp["quantity"]
            continue
        by_type[comp["type"]] = comp
        components.append(comp)

    for comp_type in sorted(covered):
        expected = counts.get(comp_type, 0)
        comp = by_type.get(comp_type)
        if comp is None and expected and comp_type in catalogue:
            components.append({"type": comp_type, "quantity": expected,
                               "mass_kg_per_unit": catalogue[comp_type]["mass"]})
        elif comp is not None and expected == 0:
            components.remove(comp)
        elif comp is not None:
            comp["quantity"] = expected
    bom["components"] = components
    return bom


def catalogue_to_bom(view: View, bom: Dict[str, Any]) -> Dict[str, Any]:
    bom = copy.deepcopy(bom)
    for comp in bom.get("components", []):
        part = view[CATALOGUE].get(comp["type"])
        if part is not None and part["mass"] is not None:
            comp["mass_kg_per_unit"] = part["mass"]
    return bom


def bom_total(view: View, bom: Dict[str, Any]) -> Dict[str, Any]:
    bom = copy.deepcopy(bom)
    bom[TOTAL] = A.compute_total_mass(bom)
    return bom


def bom_to_xml(view: View, _: str) -> str:
    return A.render_bom_xml(view[BOM])


def network_to_xml(view: View, _: str) -> str:
    return A.render_network_xml(view[NETWORK])


def report(view: View, _: str) -> str:
    return A.render_report(view[REQUIREMENTS], view[BOM])


def requirements_check(view: View) -> Dict[str, Optional[bool]]:
    return {r["requirement"]: r["satisfied"] for r in A.evaluate_requirements(view[REQUIREMENTS], view[BOM])}


# ---------------------------------------------------------------------------
# Element-level impact
# ---------------------------------------------------------------------------

def _parts(ref: str):
    parts = ref.split(":")
    return parts[0], (parts[1] if len(parts) > 1 else ""), (parts[2] if len(parts) > 2 else "")


def impacts_network_to_bom(ref, delta, view):
    artefact, element, attribute = _parts(ref)
    if delta is None:
        return []
    types = set()
    if artefact == NETWORK and "->" not in element:
        for value in (delta["old"], delta["new"]):
            if isinstance(value, dict) and "type" in value:
                types.add(value["type"])
        if attribute == "type":
            types |= {delta["old"], delta["new"]}
    elif artefact == CATALOGUE and (attribute in ("", "class")):
        types.add(element)
    return [f"{BOM}:{t}:quantity" for t in sorted(types)]


def impacts_catalogue_to_bom(ref, delta, view):
    _, element, attribute = _parts(ref)
    bom_types = {c["type"] for c in view.get(BOM, {}).get("components", [])} if BOM in view else None
    if attribute in ("mass", "") and (bom_types is None or element in bom_types):
        return [f"{BOM}:{element}:mass_kg_per_unit"]
    return []


def impacts_bom_total(ref, delta, view):
    artefact, element, _ = _parts(ref)
    return [f"{BOM}:total_system:{TOTAL}"] if artefact == BOM and element != "total_system" else []


def impacts_report(ref, delta, view):
    artefact, element, attribute = _parts(ref)
    if artefact == REQUIREMENTS:
        return [f"{REPORT}:{element}:status"]
    if element == "total_system":
        reqs = [r["RequirementID"] for r in view.get(REQUIREMENTS, []) if r["Parameter"] == attribute]
        return [f"{REPORT}:{r}:status" for r in reqs] or [f"{REPORT}:root"]
    return []


def impacts_whole(target: str, *sources: str):
    def impacts(ref, delta, view):
        return [f"{target}:root"] if ref.split(":", 1)[0] in sources else []
    return impacts


# ---------------------------------------------------------------------------
# Federation
# ---------------------------------------------------------------------------

def federation(bom_policy: str = SEMI_AUTOMATIC, network_policy: str = AUTOMATIC) -> Federation:
    return Federation(
        artefacts=[
            ArtefactSpec(CATALOGUE, "manufacturer", AUTOMATIC, catalogue_diff),
            ArtefactSpec(NETWORK, "communications_engineer", network_policy, network_diff),
            ArtefactSpec(NETWORK_XML, "communications_engineer", AUTOMATIC, _text_diff(NETWORK_XML)),
            ArtefactSpec(REQUIREMENTS, "requirements_engineer", AUTOMATIC, requirements_diff),
            ArtefactSpec(BOM, "systems_engineer", bom_policy, bom_diff),
            ArtefactSpec(BOM_XML, "systems_engineer", AUTOMATIC, _text_diff(BOM_XML)),
            ArtefactSpec(REPORT, "verification_engineer", AUTOMATIC, _text_diff(REPORT)),
        ],
        rules=[
            Rule("network_to_bom", CORRESPONDENCE, (NETWORK, CATALOGUE), BOM, network_to_bom, impacts_network_to_bom),
            Rule("catalogue_to_bom", CORRESPONDENCE, (CATALOGUE,), BOM, catalogue_to_bom, impacts_catalogue_to_bom),
            Rule("bom_total", DERIVATION, (BOM,), BOM, bom_total, impacts_bom_total),
            Rule("bom_to_xml", REPRESENTATION, (BOM,), BOM_XML, bom_to_xml, impacts_whole(BOM_XML, BOM)),
            Rule("network_to_xml", REPRESENTATION, (NETWORK,), NETWORK_XML, network_to_xml,
                 impacts_whole(NETWORK_XML, NETWORK)),
            Rule("report", CORRESPONDENCE, (REQUIREMENTS, BOM), REPORT, report, impacts_report),
        ],
        constraints=[Constraint("requirements", (REQUIREMENTS, BOM), requirements_check)],
    )


# ---------------------------------------------------------------------------
# Technology adapters (files of one version directory)
# ---------------------------------------------------------------------------

def _load_catalogue(path: Path):
    return {cid: {"mass": p.mass_kg, "class": p.part_class} for cid, p in A.load_catalogue(path).items()}


def _load_json(path: Path):
    return json.loads(path.read_text())


def _dump_network(network: Dict[str, Any]) -> str:
    return json.dumps(network, indent=2) + "\n"


LOADERS = {
    CATALOGUE: _load_catalogue, NETWORK: _load_json, NETWORK_XML: lambda p: p.read_text(),
    REQUIREMENTS: A.load_requirements, BOM: _load_json, BOM_XML: lambda p: p.read_text(),
    REPORT: lambda p: p.read_text(),
}
DUMPERS = {
    NETWORK: _dump_network, NETWORK_XML: str, BOM: A.dump_bom_json, BOM_XML: str, REPORT: str,
}


class FileStore(Store):
    """Reads and writes one artefact of a version directory in its native format."""

    def __init__(self, root: Path, artefact: str):
        self.path = Path(root) / PATHS[artefact]
        self.artefact = artefact

    def read(self) -> Any:
        return LOADERS[self.artefact](self.path)

    def write(self, state: Any) -> None:
        dump = DUMPERS.get(self.artefact)
        if dump is None:
            raise PermissionError(f"{self.artefact} is a source artefact; no rule writes it")
        text = dump(state)
        if not self.path.exists() or self.path.read_text() != text:
            self.path.write_text(text)


def load_view(root: Path) -> View:
    return {name: LOADERS[name](Path(root) / path) for name, path in PATHS.items()}


def file_stores(root: Path) -> Dict[str, FileStore]:
    return {name: FileStore(root, name) for name in PATHS}
