#!/usr/bin/env python3
"""
Command line entry point for MT.1 Change Impact & Reconciliation.

    diff      OLD NEW          detect changes between two snapshots and print the impact graph
    check     DIR [DIR ...]    exit 1 if any derived artefact is out of sync with its sources
    propagate DIR              reconcile a snapshot (BOM, XML mirrors, report) using a strategy
    run       OLD NEW          full MT.1 workflow: diff + impact analysis + propagation + verification

Examples:
    python3 tools/mt1_orchestrate.py diff v1 v2 --granularity model
    python3 tools/mt1_orchestrate.py check v1 v2
    python3 tools/mt1_orchestrate.py propagate v2 --baseline v1 --strategy automatic
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from artefacts import load_snapshot, Snapshot
from mt1_change_impact import (
    ChangeDetector,
    GranularityLevel,
    ImpactAnalyzer,
    ImpactGraph,
    PropagationEngine,
    PropagationResult,
    PropagationStrategy,
)


def _fmt(value: Any) -> str:
    return json.dumps(value) if isinstance(value, (dict, list)) else str(value)


def _section(title: str):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


class Summary:
    """Collects Markdown for $GITHUB_STEP_SUMMARY or a PR comment."""

    def __init__(self, path: Optional[str]):
        self.path = path
        self.lines: List[str] = []

    def add(self, *lines: str):
        self.lines.extend(lines)

    def write(self):
        if self.path:
            with open(self.path, "a") as f:
                f.write("\n".join(self.lines) + "\n\n")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def run_diff(old: Snapshot, new: Snapshot, granularity: GranularityLevel, summary: Summary) -> Dict[str, Any]:
    _section(f"CHANGE DETECTION: {old.root} -> {new.root}")
    changes = ChangeDetector().detect(old, new)
    for c in changes:
        print(f"  [{c.change_hash}] {c.kind.value:8} {c.node}: {_fmt(c.old_value)} -> {_fmt(c.new_value)}")
    if not changes:
        print("  No changes detected.")

    _section(f"IMPACT GRAPH ({granularity.value} level)")
    graph: ImpactGraph = ImpactAnalyzer(new).analyze(changes)
    view = graph.coarsened(granularity)
    for change in changes:
        print(f"  [{change.change_hash}] {change.node}")
        for path in graph.paths_from(change.node):
            if len(path) > 1:
                print("     " + " -> ".join(path[1:]))
    impacted = sorted({target for _, target, _ in view.edges})
    print(f"\n  Impacted {granularity.value}s ({len(impacted)}):")
    for node in impacted:
        print(f"   - {node}  (caused by {len(view.nodes[node])} change(s))")

    title = f"Changes in {new.name}" if old.name == new.name else f"Changes {old.name} → {new.name}"
    summary.add(f"### {title}", "")
    if changes:
        summary.add("| Hash | Kind | Element | Old | New |", "|---|---|---|---|---|")
        summary.add(*[f"| `{c.change_hash}` | {c.kind.value} | `{c.node}` | {_fmt(c.old_value)} | {_fmt(c.new_value)} |"
                      for c in changes])
        summary.add("", f"**Impacted {granularity.value}s:** " + (", ".join(f"`{n}`" for n in impacted) or "none"), "")
    else:
        summary.add("No changes.", "")

    return {"changes": [c.to_dict() for c in changes], "impact_graph": graph.to_dict(),
            "impacted": impacted, "granularity": granularity.value}


def print_propagation(snapshot: Snapshot, result: PropagationResult, summary: Summary, written: List[str]):
    _section(f"RECONCILIATION: {snapshot.root} (strategy: {result.strategy.value})")
    for u in result.updates:
        state = "applied" if u.applied else "PENDING"
        kind = "structural" if u.structural else u.granularity.value
        print(f"  [{state:7}] {u.artefact}:{u.element}:{u.attribute or '*'} "
              f"{_fmt(u.old_value)} -> {_fmt(u.new_value)}  ({kind}; {u.reason})")
    if result.consistent:
        print("  All derived artefacts are consistent with their sources.")
    for e in result.errors:
        print(f"  ERROR: {e}")
    for path in written:
        print(f"  wrote {snapshot.root / path}")

    _section(f"REQUIREMENT VERIFICATION: {snapshot.root}")
    for v in result.verification:
        status = {True: "SATISFIED", False: "NOT SATISFIED", None: "UNKNOWN"}[v["satisfied"]]
        print(f"  {v['requirement']}: {status} ({v['parameter']} = {v['actual']} {v['unit']}, "
              f"constraint {v['operator']} {v['threshold']})")

    if result.consistent:
        status = "✅ consistent"
    else:
        applied = len(result.updates) - len(result.pending)
        status = ", ".join(part for part in (
            f"🔄 {applied} update(s) applied" if applied else "",
            f"⚠️ {len(result.pending)} pending" if result.pending else "",
            f"❌ {len(result.errors)} error(s)" if result.errors else "",
        ) if part)
    summary.add(f"### {snapshot.name}: {status}", "")
    if result.updates:
        summary.add("| State | Element | Old | New | Reason |", "|---|---|---|---|---|")
        summary.add(*[f"| {'applied' if u.applied else 'pending'} | `{u.artefact}:{u.element}:{u.attribute or '*'}` "
                      f"| {_fmt(u.old_value)} | {_fmt(u.new_value)} | {u.reason} |" for u in result.updates])
        summary.add("")
    summary.add(*[f"- ❌ {e}" for e in result.errors])
    summary.add(*[f"- {'✅' if v['satisfied'] else '❌'} **{v['requirement']}** {v['parameter']} = {v['actual']} "
                  f"{v['unit']} ({v['operator']} {v['threshold']})" for v in result.verification], "")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_diff(args, summary: Summary) -> int:
    results = run_diff(load_snapshot(args.old), load_snapshot(args.new), GranularityLevel(args.granularity), summary)
    _write_json(args.json, results)
    return 0


def cmd_check(args, summary: Summary) -> int:
    baseline = load_snapshot(args.baseline) if args.baseline else None
    exit_code = 0
    results = {}
    for directory in args.dirs:
        snapshot = load_snapshot(directory)
        result = PropagationEngine(PropagationStrategy.MANUAL).reconcile(snapshot, baseline)
        print_propagation(snapshot, result, summary, [])
        results[str(directory)] = result.to_dict()
        if not result.consistent or (args.fail_on_violation and not result.all_satisfied):
            exit_code = 1
    _write_json(args.json, results)
    print("\nCHECK " + ("PASSED" if exit_code == 0 else "FAILED: run `propagate` to reconcile"))
    return exit_code


def cmd_propagate(args, summary: Summary) -> int:
    snapshot = load_snapshot(args.dir)
    baseline = load_snapshot(args.baseline) if args.baseline else None
    engine = PropagationEngine(PropagationStrategy(args.strategy))
    result = engine.reconcile(snapshot, baseline)
    written = engine.apply(snapshot, result)
    print_propagation(snapshot, result, summary, written)
    _write_json(args.json, result.to_dict())
    if result.errors or (args.fail_on_violation and not result.all_satisfied):
        return 1
    return 0


def cmd_run(args, summary: Summary) -> int:
    old, new = load_snapshot(args.old), load_snapshot(args.new)
    results: Dict[str, Any] = {"workflow": "MT.1 -- Change Impact & Reconciliation", "strategy": args.strategy}
    results.update(run_diff(old, new, GranularityLevel(args.granularity), summary))
    engine = PropagationEngine(PropagationStrategy(args.strategy))
    result = engine.reconcile(new, old)
    written = engine.apply(new, result)
    print_propagation(new, result, summary, written)
    results["propagation"] = result.to_dict()
    results["written"] = written
    _write_json(args.json, results)
    return 1 if result.errors else 0


def _write_json(path: Optional[str], data: Dict[str, Any]):
    if path:
        Path(path).write_text(json.dumps(data, indent=2, default=str) + "\n")
        print(f"\nResults saved to: {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", help="write machine-readable results to this file")
    parser.add_argument("--summary", help="append a Markdown summary to this file (e.g. $GITHUB_STEP_SUMMARY)")
    sub = parser.add_subparsers(dest="command", required=True)

    strategies = [s.value for s in PropagationStrategy]
    granularities = [g.value for g in GranularityLevel]

    p = sub.add_parser("diff", help="detect changes between two snapshots")
    p.add_argument("old", type=Path)
    p.add_argument("new", type=Path)
    p.add_argument("--granularity", choices=granularities, default="element")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("check", help="verify that derived artefacts are in sync")
    p.add_argument("dirs", type=Path, nargs="+")
    p.add_argument("--baseline", type=Path)
    p.add_argument("--fail-on-violation", action="store_true", help="also fail when a requirement is violated")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("propagate", help="reconcile one snapshot")
    p.add_argument("dir", type=Path)
    p.add_argument("--baseline", type=Path, help="previous snapshot, used to detect removed comms components")
    p.add_argument("--strategy", choices=strategies, default="semi-automatic")
    p.add_argument("--fail-on-violation", action="store_true")
    p.set_defaults(func=cmd_propagate)

    p = sub.add_parser("run", help="full MT.1 workflow from OLD to NEW")
    p.add_argument("old", type=Path)
    p.add_argument("new", type=Path)
    p.add_argument("--strategy", choices=strategies, default="semi-automatic")
    p.add_argument("--granularity", choices=granularities, default="element")
    p.set_defaults(func=cmd_run)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    summary = Summary(args.summary)
    code = args.func(args, summary)
    summary.write()
    return code


if __name__ == "__main__":
    sys.exit(main())
