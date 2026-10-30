#!/usr/bin/env python3
"""
Loaders and writers for the five satellite configuration artefacts.

Every artefact keeps its own format (OWL, JSON, CSV, Markdown); this module only
reads them into plain Python structures and renders the derived representations
(bom.xml, comm-network.xml, report.md) back in the exact layout used in the repo.

Source artefacts (owned by an engineering role, never rewritten by the tools):
    manufacturer/catalogue.owl
    communications_engineer/comm-network.json
    requirements_engineer/requirements.csv

Artefacts maintained by propagation:
    systems_engineer/bom.json            (masses from catalogue, comms parts from network, total)
    systems_engineer/bom.xml             (mirror of bom.json)
    communications_engineer/comm-network.xml (mirror of comm-network.json)
    verification_engineer/report.md      (requirements evaluated against bom.json)
"""

import csv
import copy
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SYSTEM_NAME = "SatAlpha"

CATALOGUE = "manufacturer/catalogue.owl"
BOM_JSON = "systems_engineer/bom.json"
BOM_XML = "systems_engineer/bom.xml"
NETWORK_JSON = "communications_engineer/comm-network.json"
NETWORK_XML = "communications_engineer/comm-network.xml"
REQUIREMENTS = "requirements_engineer/requirements.csv"
REPORT = "verification_engineer/report.md"

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "vim4": "http://bipm.org/jcgm/vim4#",
    "sat": "http://www.example.com/satellite#",
}
RDF_ABOUT = f"{{{NS['rdf']}}}about"
RDF_RESOURCE = f"{{{NS['rdf']}}}resource"
MASS_QUANTITY = "http://iso.org/iso-80000-4.1#mass"
KILOGRAM = "http://iso.org/iso-80000-4.1#kilogram"


@dataclass
class Part:
    """A component individual from the Parts Catalogue."""
    component_id: str
    iri: str
    part_class: str
    mass_kg: Optional[float] = None


@dataclass
class Snapshot:
    """All artefacts of one version directory (e.g. v1/ or v2/)."""
    root: Path
    catalogue: Dict[str, Part]
    bom: Dict[str, Any]
    bom_xml_text: Optional[str]
    network: Dict[str, Any]
    network_xml_text: Optional[str]
    requirements: List[Dict[str, str]]
    report_text: Optional[str]
    problems: List[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.root.name


# ---------------------------------------------------------------------------
# Parts Catalogue (OWL / RDF-XML)
# ---------------------------------------------------------------------------

def load_catalogue(path: Path) -> Dict[str, Part]:
    """
    Read component masses from the catalogue.

    Mass is modelled with VIM4 as a chain of individuals:
        <value> vim4:isAttributedTo <quantity>
        <quantity> vim4:characterizes <component> ; vim4:instantiates iso:mass
        <component> sat:componentID "T001"
    """
    root = ET.parse(path).getroot()
    individuals = {
        ind.get(RDF_ABOUT): ind
        for ind in root.iter(f"{{{NS['owl']}}}NamedIndividual")
        if ind.get(RDF_ABOUT)
    }

    parts: Dict[str, Part] = {}
    for iri, ind in individuals.items():
        component_id = ind.findtext("sat:componentID", namespaces=NS)
        if not component_id:
            continue
        part_class = ""
        for type_elem in ind.findall("rdf:type", NS):
            resource = type_elem.get(RDF_RESOURCE, "")
            if resource.startswith(NS["sat"]):
                part_class = resource[len(NS["sat"]):]
        parts[iri] = Part(component_id.strip(), iri, part_class)

    mass_quantities: Dict[str, str] = {}
    for iri, ind in individuals.items():
        characterizes = ind.find("vim4:characterizes", NS)
        instantiates = ind.find("vim4:instantiates", NS)
        if characterizes is not None and instantiates is not None \
                and instantiates.get(RDF_RESOURCE) == MASS_QUANTITY:
            mass_quantities[iri] = characterizes.get(RDF_RESOURCE)

    for iri, ind in individuals.items():
        attributed_to = ind.find("vim4:isAttributedTo", NS)
        number = ind.findtext("vim4:hasDoubleNumber", namespaces=NS)
        if attributed_to is None or number is None:
            continue
        owner = mass_quantities.get(attributed_to.get(RDF_RESOURCE))
        if owner not in parts:
            continue
        unit = ind.find("vim4:unit", NS)
        if unit is None or unit.get(RDF_RESOURCE) != KILOGRAM:
            raise ValueError(f"{iri}: mass value is not expressed in kilograms")
        parts[owner].mass_kg = float(number)

    by_id: Dict[str, Part] = {}
    for part in parts.values():
        if part.component_id in by_id:
            raise ValueError(f"Duplicate componentID {part.component_id} in {path}")
        by_id[part.component_id] = part
    return by_id


# ---------------------------------------------------------------------------
# Bill of Materials (JSON + XML mirror)
# ---------------------------------------------------------------------------

def fmt_number(value: Any) -> str:
    """Format numbers the way bom.xml does: 85.0 -> '85', 22.5 -> '22.5'."""
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def compute_total_mass(bom: Dict[str, Any]) -> float:
    return float(sum(c["quantity"] * c["mass_kg_per_unit"] for c in bom.get("components", [])))


def dump_bom_json(bom: Dict[str, Any]) -> str:
    return json.dumps(bom, indent=2) + "\n"


def render_bom_xml(bom: Dict[str, Any]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8" ?>', " <root>"]
    for comp in bom.get("components", []):
        lines += [
            "     <components>",
            f"         <type>{comp['type']}</type>",
            f"         <quantity>{fmt_number(comp['quantity'])}</quantity>",
            f"         <mass_kg_per_unit>{fmt_number(comp['mass_kg_per_unit'])}</mass_kg_per_unit>",
            "     </components>",
        ]
    lines.append(f"     <calculated_total_mass_kg>{fmt_number(bom['calculated_total_mass_kg'])}</calculated_total_mass_kg>")
    lines.append(" </root>")
    return "\n".join(lines) + "\n"


def parse_bom_xml(text: str) -> Dict[str, Any]:
    root = ET.fromstring(text)
    components = [
        {
            "type": comp.findtext("type"),
            "quantity": float(comp.findtext("quantity")),
            "mass_kg_per_unit": float(comp.findtext("mass_kg_per_unit")),
        }
        for comp in root.findall("components")
    ]
    return {"components": components, "calculated_total_mass_kg": float(root.findtext("calculated_total_mass_kg"))}


def bom_equivalent(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Semantic equality of two BOMs (numbers compared as floats)."""
    def norm(bom):
        return (
            [(c["type"], float(c["quantity"]), float(c["mass_kg_per_unit"])) for c in bom.get("components", [])],
            float(bom.get("calculated_total_mass_kg", 0)),
        )
    return norm(a) == norm(b)


# ---------------------------------------------------------------------------
# Network Topology (JSON + XML mirror)
# ---------------------------------------------------------------------------

def render_network_xml(network: Dict[str, Any]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8" ?>', " <root>",
             f"     <subsystem>{network['subsystem']}</subsystem>"]
    for node in network.get("nodes", []):
        lines += ["     <nodes>"] + [f"         <{k}>{v}</{k}>" for k, v in node.items()] + ["     </nodes>"]
    for link in network.get("links", []):
        lines += ["     <links>"] + [f"         <{k}>{v}</{k}>" for k, v in link.items()] + ["     </links>"]
    lines.append(" </root>")
    return "\n".join(lines) + "\n"


def parse_network_xml(text: str) -> Dict[str, Any]:
    root = ET.fromstring(text)
    return {
        "subsystem": root.findtext("subsystem"),
        "nodes": [{child.tag: child.text for child in node} for node in root.findall("nodes")],
        "links": [{child.tag: child.text for child in link} for link in root.findall("links")],
    }


# ---------------------------------------------------------------------------
# Requirements (CSV) and Report (Markdown)
# ---------------------------------------------------------------------------

def load_requirements(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def evaluate_constraint(actual: float, operator: str, threshold: float) -> bool:
    """Evaluate a requirement constraint; unknown operators never pass."""
    operations = {
        "<=": actual <= threshold,
        ">=": actual >= threshold,
        "<": actual < threshold,
        ">": actual > threshold,
        "==": actual == threshold,
    }
    return operations.get(operator, False)


def evaluate_requirements(requirements: List[Dict[str, str]], bom: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Evaluate each requirement against the matching numeric field of the BOM."""
    results = []
    for req in requirements:
        parameter = req["Parameter"]
        actual = bom.get(parameter)
        satisfied = None
        if isinstance(actual, (int, float)):
            satisfied = evaluate_constraint(float(actual), req["Operator"], float(req["Value"]))
        results.append({
            "requirement": req["RequirementID"],
            "description": req["Description"],
            "parameter": parameter,
            "operator": req["Operator"],
            "threshold": req["Value"],
            "unit": req.get("Unit", ""),
            "actual": actual,
            "satisfied": satisfied,
        })
    return results


def render_report(requirements: List[Dict[str, str]], bom: Dict[str, Any]) -> str:
    lines = [
        f"# System Verification Report - {SYSTEM_NAME}",
        "",
        "| Requirement ID | Description | Parameter | Constraint | Actual Value | Unit | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in evaluate_requirements(requirements, bom):
        actual = repr(float(r["actual"])) if r["satisfied"] is not None else "N/A"
        status = {True: "SATISFIED", False: "NOT SATISFIED", None: "UNKNOWN PARAMETER"}[r["satisfied"]]
        lines.append(
            f"| {r['requirement']} | {r['description']} | {r['parameter']} | "
            f"{r['operator']} {r['threshold']} | {actual} | {r['unit']} | {status} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Snapshot I/O
# ---------------------------------------------------------------------------

def _read_optional(path: Path) -> Optional[str]:
    return path.read_text() if path.exists() else None


def load_snapshot(root: Path) -> Snapshot:
    root = Path(root)
    with open(root / BOM_JSON) as f:
        bom = json.load(f)
    with open(root / NETWORK_JSON) as f:
        network = json.load(f)
    return Snapshot(
        root=root,
        catalogue=load_catalogue(root / CATALOGUE),
        bom=bom,
        bom_xml_text=_read_optional(root / BOM_XML),
        network=network,
        network_xml_text=_read_optional(root / NETWORK_XML),
        requirements=load_requirements(root / REQUIREMENTS),
        report_text=_read_optional(root / REPORT),
    )


def write_artefacts(root: Path, contents: Dict[str, str]) -> List[str]:
    """Write rendered artefacts (relative path -> text); returns paths that changed."""
    written = []
    for rel_path, text in contents.items():
        path = Path(root) / rel_path
        if path.exists() and path.read_text() == text:
            continue
        path.write_text(text)
        written.append(rel_path)
    return written


def clone_bom(bom: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(bom)
