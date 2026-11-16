"""
Replay of the satellite scenarios for demonstrations.

Runs the engineering scenarios on the in-memory realisation, records every
published message and every human action together with a snapshot of the
federation, and writes a self-contained HTML page (replay.html) that plays the
recording back step by step.

    python3 -m femm replay [--out femm-replay.html] [--json trace.json]
"""

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import messages as M
from . import satellite as S
from .bus import InMemoryBus
from .federation import ImpactGraph
from .runtime import System

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "replay.html"

Edit = Tuple[str, Any, Optional[int], str, str]      # artefact, state, base version, author, narration


def snapshot(system: System) -> Dict[str, Any]:
    view = system.view()
    return {
        "msas": {n: {"phase": m.phase, "version": m.version, "queue": len(m.queue)} for n, m in system.msas.items()},
        "total": view[S.BOM].get(S.TOTAL),
        "bom": [[c["type"], c["quantity"], c["mass_kg_per_unit"]] for c in view[S.BOM]["components"]],
        "t001": view[S.CATALOGUE]["T001"]["mass"],
        "nodes": [[n["component_id"], n["type"]] for n in view[S.NETWORK]["nodes"]],
        "req": dict(system.validation.results),
        "consistent": system.consistent(),
    }


def summarise(message: M.Message) -> Dict[str, Any]:
    """The parts of a message's data the page shows; full states are left out."""
    data = message.data or {}
    out = {k: data[k] for k in ("structural", "policy", "reason", "author", "by") if data.get(k) is not None}
    if "deltas" in data:
        out["deltas"] = [{"kind": d["kind"], "ref": d["ref"], "old": d.get("old"), "new": d.get("new")}
                         for d in data["deltas"]][:8]
    if message.type == M.IMPACT:
        out["targets"] = sorted(ImpactGraph.from_dict(data["graph"]).artefacts)
    elif message.type == M.BASELINE:
        out["versions"] = data.get("versions")
    elif message.type == M.EVALUATED:
        out.update(requirement=data.get("requirement"), satisfied=data.get("satisfied"),
                   previous=data.get("previous"))
    return out


def record(key: str, title: str, description: str, edits: List[Edit], approve: bool = True,
           shuffle: bool = False, seed: int = 0, duplicates: int = 0) -> Dict[str, Any]:
    """Run one scenario and return its frames: one per published message or human action."""
    v1 = S.load_view(REPO / "v1")
    M.reset_ids()
    bus = InMemoryBus(seed, shuffle=shuffle, duplicates=duplicates)
    system = System(S.federation(), v1, bus)
    frames: List[Dict[str, Any]] = []
    publish = bus.publish

    def recording_publish(topic: str, message: M.Message) -> None:
        publish(topic, message)
        frame = {"kind": "message", "topic": topic, "id": message.id, "type": message.type.split("femm.", 1)[1],
                 "source": message.source, "artefact": message.artefact, "version": message.version,
                 "base": message.base_version, "plan": message.correlation_id, "cause": message.causation_id,
                 "data": summarise(message), "state": snapshot(system)}
        if message.type == M.EVALUATED:          # Validation updates its results after publishing
            frame["state"]["req"][message.data["requirement"]] = message.data["satisfied"]
        frames.append(frame)
    bus.publish = recording_publish

    def action(text: str, who: str, artefact: Optional[str] = None) -> None:
        frames.append({"kind": "action", "text": text, "who": who, "artefact": artefact, "state": snapshot(system)})

    action("Federation at v1, consistent", "start")
    for artefact, state, base, author, text in edits:
        action(text, author, artefact)
        system.edit(artefact, state, base, author)
    system.run()
    while system.awaiting():
        for name, command in system.awaiting().items():
            deltas = ", ".join(f"{d['kind']} {d['ref']}" for d in command.data["deltas"])
            owner = system.federation.artefacts[name].owner
            if approve:
                action(f"{owner} approves {command.id} ({deltas})", owner, name)
                system.approve(name, by=owner)
            else:
                action(f"{owner} rejects {command.id} ({deltas})", owner, name)
                system.reject(name, "rejected in the demo", by=owner)
        system.run()
    action("Quiescent: " + ("consistent" if system.consistent() else "inconsistent (rejected change)"), "end")
    return {"key": key, "title": title, "description": description, "frames": frames,
            "summary": {"deliveries": bus.delivered, "messages": len(bus.log), "lost": system.lost_updates,
                        "consistent": system.consistent(), "total": system.view()[S.BOM].get(S.TOTAL),
                        "baselines": len(system.versions.baselines) - 1}}


def scenarios() -> List[Dict[str, Any]]:
    v1, v2 = S.load_view(REPO / "v1"), S.load_view(REPO / "v2")
    alt = copy.deepcopy(v1[S.NETWORK])
    alt["nodes"] = [n for n in alt["nodes"] if n["type"] != "ANT001"] + [
        {"component_id": f"AntennaB{i}", "type": "ANT002", "role": "transceiver"} for i in (1, 2)]
    cat = (S.CATALOGUE, v2[S.CATALOGUE], None, "manufacturer", "Manufacturer changes thruster T001 mass 85 → 95 kg")
    net = (S.NETWORK, v2[S.NETWORK], None, "communications_engineer",
           "Communications engineer renames the antenna and adds backup antenna ANT002")

    def faulty() -> Dict[str, Any]:
        """The first seed whose schedule duplicates deliveries and produces a conflict."""
        for seed in range(100):
            run = record("chaos", "ES.1 + ES.2, faulty bus", "Same edits, but the bus delivers in random order and "
                         "duplicates deliveries. MSAs ignore duplicates; outdated commands become conflicts and are "
                         "replanned.", [cat, net], shuffle=True, seed=seed, duplicates=5)
            if any(f.get("type") == "conflict.detected" for f in run["frames"]) and run["summary"]["deliveries"] > 141:
                return run
        raise RuntimeError("no faulty schedule with a conflict found")

    return [
        record("es1", "ES.1 Catalogue change", "A mass change in the parts catalogue propagates to BoM, its XML and "
               "the verification report without anyone being asked (value change, semi-automatic BoM).", [cat]),
        record("es2", "ES.2 Network change", "Adding an antenna is structural: the BoM owner must approve the "
               "command before it is applied.", [net]),
        record("es3", "ES.3 Concurrent edits", "Two engineers edit the network from the same base version. The "
               "second edit is reported as a conflict and not written.", [
                   (S.NETWORK, v2[S.NETWORK], 1, "engineer_1", "Engineer 1 edits network (base v1)"),
                   (S.NETWORK, alt, 1, "engineer_2", "Engineer 2 edits network, also from base v1")]),
        record("all", "ES.1 + ES.2 → v2", "Both changes together reach the v2 configuration.", [cat, net]),
        faulty(),
        record("reject", "ES.2 rejected", "The systems engineer rejects the structural BoM change. Nothing is "
               "written; the federation stays inconsistent until someone acts.", [net], approve=False),
    ]


def trace() -> Dict[str, Any]:
    federation = S.federation()
    meta = {"artefacts": [{"name": a.name, "owner": a.owner, "policy": a.policy, "path": S.PATHS[a.name]}
                          for a in federation.artefacts.values()],
            "rules": [{"name": r.name, "sources": list(r.sources), "target": r.target, "kind": r.kind}
                      for r in federation.rules]}
    return {"meta": meta, "scenarios": scenarios()}


def page(data: Dict[str, Any]) -> str:
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    return TEMPLATE.read_text().replace("__DATA__", payload)
