"""
Command line interface.

    python3 -m femm scenario es1|es2|es3|all [--shuffle] [--seed N] [--duplicates K] [--reject]
    python3 -m femm check DIR
    python3 -m femm git ...              (Git realisation, see femm/gitflow.py)
"""

import argparse
import copy
import json
import sys
from pathlib import Path

from . import messages as M
from . import satellite as S
from .bus import InMemoryBus
from .runtime import System

REPO = Path(__file__).resolve().parent.parent


def scenario(args) -> int:
    v1, v2 = S.load_view(REPO / "v1"), S.load_view(REPO / "v2")
    bus = InMemoryBus(args.seed, shuffle=args.shuffle, duplicates=args.duplicates)
    system = System(S.federation(), v1, bus)
    steps = {"es1": [(S.CATALOGUE, v2[S.CATALOGUE], None, "manufacturer")],
             "es2": [(S.NETWORK, v2[S.NETWORK], None, "communications engineer")]}
    if args.name == "es3":
        alt = copy.deepcopy(v1[S.NETWORK])
        alt["nodes"] = [n for n in alt["nodes"] if n["type"] != "ANT001"] + [
            {"component_id": f"AntennaB{i}", "type": "ANT002", "role": "transceiver"} for i in (1, 2)]
        base = system.msas[S.NETWORK].version
        edits = [(S.NETWORK, v2[S.NETWORK], base, "engineer 1"), (S.NETWORK, alt, base, "engineer 2")]
    elif args.name == "all":
        edits = steps["es1"] + steps["es2"]
    else:
        edits = steps[args.name]

    for artefact, state, base, author in edits:
        print(f"# {author} edits {artefact}")
        system.edit(artefact, state, base, author)
    system.run()
    while system.awaiting():
        for name, command in system.awaiting().items():
            deltas = ", ".join(f"{d['kind']} {d['ref']}" for d in command.data["deltas"])
            decision = "rejects" if args.reject else "approves"
            print(f"# owner of {name} {decision} {command.id}: {deltas}")
            system.reject(name, "rejected on the command line") if args.reject else system.approve(name)
        system.run()

    print("\n".join(system.trace()))
    print()
    bom = system.view()[S.BOM]
    print(f"total mass: {bom['calculated_total_mass_kg']} kg; requirements: {system.validation.results}")
    print(f"consistent: {system.consistent()} (violations: {system.violations()})")
    print(f"baselines: {[b['versions'] for b in system.versions.baselines[1:]]}")
    print(f"deliveries: {bus.delivered}, messages: {len(bus.log)}, lost updates: {system.lost_updates}")
    if args.write:
        out = Path(args.write)
        for name, path in S.PATHS.items():
            target = out / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if name in S.DUMPERS:
                target.write_text(S.DUMPERS[name](system.view()[name]))
        print(f"derived artefacts written to {out}")
    return 0 if system.consistent() or args.reject else 1


def check(args) -> int:
    federation = S.federation()
    view = S.load_view(Path(args.dir))
    violations = federation.violations(view)
    results = federation.evaluate(view)
    print(json.dumps({"consistent": not violations, "violations": violations, "requirements": results}, indent=2))
    return 0 if not violations else 1


def demo_git(args) -> int:
    """ES.1 and ES.2 on a throw-away Git repository, with pull requests simulated locally."""
    import shutil
    import subprocess
    import tempfile

    from .gitflow import Git, GitPlanner, LocalForge

    human = {"GIT_AUTHOR_NAME": "Engineer", "GIT_AUTHOR_EMAIL": "engineer@example.com",
             "GIT_COMMITTER_NAME": "Engineer", "GIT_COMMITTER_EMAIL": "engineer@example.com"}
    repo = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="femm-demo-")) / "repo"
    if repo.exists():
        print(f"{repo} exists; choose another directory", file=sys.stderr)
        return 1
    shutil.copytree(REPO / "v1", repo / "fed")
    (repo / "femm.json").write_text(json.dumps({"module": "femm.satellite", "root": "fed", "main": "main"}))
    git = Git(repo)
    git.run("init", "-q", "-b", "main")
    git.run("add", "-A")
    git.run("commit", "-q", "-m", "v1 of the satellite configuration", env=human)
    forge = LocalForge(git)

    def push(message, rel):
        before = git.sha("main")
        shutil.copy(REPO / "v2" / rel, repo / "fed" / rel)
        git.run("commit", "-q", "-am", message, env=human)
        print(f"\n$ git commit -m '{message}'")
        result = GitPlanner(repo, forge).on_push(before, git.sha("main"))
        print("\n".join("  " + line for line in result["log"]))
        return result

    push("manufacturer: thruster T001 is 95 kg (ES.1)", S.PATHS[S.CATALOGUE])
    push("comms: rename antenna, add backup antenna ANT002 (ES.2)", S.PATHS[S.NETWORK])
    for pr in forge.list_open():
        print(f"\n# systems engineer approves pull request #{pr.id} ({pr.branch}) by merging it")
        before = git.sha("main")
        after = forge.merge(pr)
        result = GitPlanner(repo, forge).on_push(before, after)
        print("\n".join("  " + line for line in result["log"]))
    print("\n$ git log --oneline")
    print(git.run("log", "--oneline", "--decorate"))
    same = all((repo / "fed" / p).read_text() == (REPO / "v2" / p).read_text() for p in S.PATHS.values())
    print(f"\nfed/ equals v2/: {same}\nrepository: {repo}")
    return 0 if same else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m femm")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("scenario", help="run an engineering scenario on the in-memory realisation")
    p.add_argument("name", choices=["es1", "es2", "es3", "all"])
    p.add_argument("--shuffle", action="store_true", help="deliver messages in random order")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--duplicates", type=int, default=0, help="number of duplicated deliveries")
    p.add_argument("--reject", action="store_true", help="owners reject commands that need approval")
    p.add_argument("--write", metavar="DIR", help="write the resulting artefacts to DIR")
    p.set_defaults(func=scenario)
    p = sub.add_parser("check", help="check consistency of a version directory")
    p.add_argument("dir")
    p.set_defaults(func=check)
    p = sub.add_parser("demo-git", help="run ES.1 and ES.2 on a throw-away Git repository")
    p.add_argument("--dir", help="where to create the repository (must not exist)")
    p.set_defaults(func=demo_git)
    sub.add_parser("git", help="Git realisation: python3 -m femm git --help")
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["git"]:
        from .gitflow import main as git_main
        return git_main(argv[1:])
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
