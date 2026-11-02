"""
Git realisation of the protocol.

The repository is the bus and the store; pull requests are commands:

    artefact            a file under the federation root, owned by one team (CODEOWNERS)
    version             the Git blob id of the file on the main branch
    event               a push to main that changes the file
    command             a pull request that changes one artefact, opened by the planner;
                        it carries a command record  .femm/log/<plan>/<command>.json
    approval            merging the pull request (by the owner, or at once by the
                        planner when the owner's policy allows it)
    rejection           closing the pull request
    base-version check  the blob of the target on main must still be the blob the
                        command was computed against; otherwise the pull request is
                        closed as a conflict and the step is replanned
    correlation (A4)    the command record merged with a change names its plan
    sequential (A2)     one planner run at a time (workflow concurrency group)
    baseline            a tag  femm-baseline-<n>  on a consistent main without open commands

The federation is described in  femm.json  at the root of the repository:

    {"module": "femm.satellite", "root": "v2", "main": "main",
     "owners": {"systems_engineer": ["@alice"], ...}}

where the module provides  federation(), PATHS, load_view(root)  and  file_stores(root).
"""

import argparse
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .federation import AUTOMATIC, SEMI_AUTOMATIC, Federation, canonical, is_structural

LOG_DIR = ".femm/log"
BRANCH_PREFIX = "femm/"
LABEL = "femm"
BOT = {"GIT_AUTHOR_NAME": "femm-planner", "GIT_AUTHOR_EMAIL": "femm-planner@users.noreply.github.com",
       "GIT_COMMITTER_NAME": "femm-planner", "GIT_COMMITTER_EMAIL": "femm-planner@users.noreply.github.com"}
ZERO = "0" * 40
CURSOR = "refs/femm/processed"       # last commit of main the planner has handled


# ---------------------------------------------------------------------------
# Git and forges
# ---------------------------------------------------------------------------

class Git:
    def __init__(self, repo: Path):
        self.repo = Path(repo)

    def run(self, *args: str, env: Optional[Dict[str, str]] = None, cwd: Optional[Path] = None,
            binary: bool = False):
        result = subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True,
                                env={**os.environ, **(env or {})}, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {result.stderr.decode().strip()}")
        return result.stdout if binary else result.stdout.decode().strip()

    def sha(self, ref: str) -> str:
        return self.run("rev-parse", ref)

    def blob(self, ref: str, path: str) -> Optional[str]:
        try:
            return self.run("rev-parse", f"{ref}:{path}")
        except RuntimeError:
            return None

    def show(self, ref: str, path: str) -> Optional[str]:
        try:
            return self.run("show", f"{ref}:{path}")
        except RuntimeError:
            return None

    def added(self, before: str, after: str, prefix: str) -> List[str]:
        out = self.run("diff", "--name-only", "--diff-filter=A", before, after, "--", prefix)
        return [line for line in out.splitlines() if line]

    def extract(self, ref: str, path: str, dest: Path) -> None:
        data = self.run("archive", "--format=tar", ref, "--", path, binary=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(dest, filter="data")
            else:                                     # Python < 3.12
                tar.extractall(dest)


@dataclass
class PullRequest:
    id: str
    branch: str
    title: str = ""


class Forge:
    """Where branches are pushed and pull requests live."""
    main_ref = "main"

    def __init__(self, git: Git):
        self.git = git

    def head(self) -> str:
        return self.git.sha(self.main_ref)

    def ref(self, pr: PullRequest) -> str:
        return pr.branch

    def push(self, branch: str) -> None: ...
    def push_tag(self, tag: str) -> None: ...

    def cursor(self) -> Optional[str]:
        try:
            return self.git.sha(CURSOR)
        except RuntimeError:
            return None

    def set_cursor(self, sha: str) -> None:
        self.git.run("update-ref", CURSOR, sha)
    def open_pr(self, branch: str, title: str, body: str) -> PullRequest: raise NotImplementedError
    def list_open(self) -> List[PullRequest]: raise NotImplementedError
    def merge(self, pr: PullRequest) -> str: raise NotImplementedError
    def close(self, pr: PullRequest, comment: str) -> None: raise NotImplementedError


class LocalForge(Forge):
    """Pull requests as local branches; merging squashes into the local main branch (tests, demos)."""

    def __init__(self, git: Git, main: str = "main"):
        super().__init__(git)
        self.main_ref = main
        self.prs: Dict[str, PullRequest] = {}
        self.closed: Dict[str, str] = {}             # id -> comment
        self.merged: List[str] = []
        self._next = 1

    def open_pr(self, branch, title, body):
        pr = PullRequest(str(self._next), branch, title)
        self._next += 1
        self.prs[pr.id] = pr
        return pr

    def list_open(self):
        return list(self.prs.values())

    def merge(self, pr):
        self.git.run("checkout", "-q", self.main_ref)
        self.git.run("merge", "--squash", "-q", pr.branch)
        self.git.run("commit", "-q", "-m", f"{pr.title} (#{pr.id})", env=BOT)
        self.git.run("branch", "-q", "-D", pr.branch)
        del self.prs[pr.id]
        self.merged.append(pr.id)
        return self.head()

    def close(self, pr, comment):
        self.git.run("branch", "-q", "-D", pr.branch)
        del self.prs[pr.id]
        self.closed[pr.id] = comment


class GitHubForge(Forge):
    """Pull requests on GitHub via the gh CLI (GH_TOKEN must be set)."""
    main_ref = "origin/main"

    def __init__(self, git: Git, remote: str = "origin", main: str = "main"):
        super().__init__(git)
        self.remote, self.main = remote, main
        self.main_ref = f"{remote}/{main}"
        self._label = False

    def gh(self, *args: str) -> str:
        result = subprocess.run(["gh", *args], cwd=self.git.repo, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout.strip()

    def head(self):
        self.git.run("fetch", "-q", self.remote, self.main)
        return self.git.sha(self.main_ref)

    def ref(self, pr):
        self.git.run("fetch", "-q", self.remote, f"{pr.branch}:refs/remotes/{self.remote}/{pr.branch}")
        return f"{self.remote}/{pr.branch}"

    def push(self, branch):
        self.git.run("push", "-q", self.remote, branch)

    def push_tag(self, tag):
        self.git.run("push", "-q", self.remote, tag)

    def cursor(self):
        try:
            self.git.run("fetch", "-q", self.remote, f"+{CURSOR}:{CURSOR}")
        except RuntimeError:
            return None
        return super().cursor()

    def set_cursor(self, sha):
        super().set_cursor(sha)
        self.git.run("push", "-q", "--force", self.remote, f"{CURSOR}:{CURSOR}")

    def open_pr(self, branch, title, body):
        if not self._label:
            self.gh("label", "create", LABEL, "--force", "--color", "5319e7",
                    "--description", "Command proposed by the Femm planner")
            self._label = True
        url = self.gh("pr", "create", "--head", branch, "--base", self.main, "--title", title,
                      "--body", body, "--label", LABEL)
        return PullRequest(url.rstrip("/").rsplit("/", 1)[-1], branch, title)

    def list_open(self):
        data = json.loads(self.gh("pr", "list", "--label", LABEL, "--state", "open",
                                  "--json", "number,headRefName,title"))
        return [PullRequest(str(p["number"]), p["headRefName"], p["title"]) for p in data
                if p["headRefName"].startswith(BRANCH_PREFIX)]

    def merge(self, pr):
        self.gh("pr", "merge", pr.id, "--squash", "--delete-branch")
        return self.head()

    def close(self, pr, comment):
        self.gh("pr", "close", pr.id, "--comment", comment, "--delete-branch")


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

def load_config(repo: Path) -> Dict[str, Any]:
    config = json.loads((Path(repo) / "femm.json").read_text())
    config.setdefault("main", "main")
    return config


class GitPlanner:
    def __init__(self, repo: Path, forge: Forge, config: Optional[Dict[str, Any]] = None):
        self.git = forge.git
        self.forge = forge
        self.config = config or load_config(repo)
        self.module = importlib.import_module(self.config["module"])
        self.federation: Federation = self.module.federation()
        self.root = self.config["root"]
        self.report: List[str] = []

    # -- views ---------------------------------------------------------------------

    def path(self, artefact: str) -> str:
        return f"{self.root}/{self.module.PATHS[artefact]}"

    def view_at(self, ref: str) -> Dict[str, Any]:
        tmp = Path(tempfile.mkdtemp(prefix="femm-view-"))
        try:
            self.git.extract(ref, self.root, tmp)
            return self.module.load_view(tmp / self.root)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def records(self, before: str, after: str) -> Dict[str, Dict[str, Any]]:
        """Command records merged between two commits, by target artefact."""
        found = {}
        for path in self.git.added(before, after, LOG_DIR):
            record = json.loads(self.git.show(after, path))
            found[record["target"]] = record
        return found

    def pr_records(self, pr: PullRequest) -> List[Dict[str, Any]]:
        ref = self.forge.ref(pr)
        base = self.git.run("merge-base", self.forge.main_ref, ref)
        return [json.loads(self.git.show(ref, p)) for p in self.git.added(base, ref, LOG_DIR)]

    # -- planning ------------------------------------------------------------------

    def events(self, before: str, after: str) -> List[Dict[str, Any]]:
        old, new = self.view_at(before), self.view_at(after)
        records = self.records(before, after)
        events = []
        for artefact in self.federation.order:
            if canonical(old[artefact]) == canonical(new[artefact]):
                continue
            record = records.get(artefact)
            events.append({
                "artefact": artefact, "version": self.git.blob(after, self.path(artefact)),
                "deltas": self.federation.deltas(artefact, old[artefact], new[artefact]),
                "plan": record["plan"] if record else f"p-{after[:8]}",
                "causation": record["id"] if record else f"commit:{after[:8]}",
            })
        return events

    def command(self, view: Dict[str, Any], target: str, after: str, plan: str, causation: str,
                suffix: str = "") -> Optional[Tuple[Dict[str, Any], Any]]:
        new = self.federation.compute(target, view)
        if canonical(new) == canonical(view[target]):
            return None
        deltas = self.federation.deltas(target, view[target], new)
        spec = self.federation.artefacts[target]
        record = {
            "id": f"c-{after[:8]}-{target}{suffix}", "plan": plan, "target": target,
            "path": self.path(target), "base_commit": after, "base_blob": self.git.blob(after, self.path(target)),
            "causation": causation, "owner": spec.owner, "policy": spec.policy,
            "structural": is_structural(deltas), "deltas": deltas,
        }
        return record, new

    def plan(self, before: str, after: str) -> List[Tuple[Dict[str, Any], Any]]:
        """Commands for the changes between two commits of main (one per target)."""
        view = self.view_at(after)
        commands: Dict[str, Tuple[Dict[str, Any], Any]] = {}
        for event in self.events(before, after):
            artefact = event["artefact"]
            self.report.append(f"event   {artefact} changed ({len(event['deltas'])} deltas, plan {event['plan']}, "
                               f"cause {event['causation']})")
            affected = self.federation.impact(event["deltas"], view).artefacts
            targets = self.federation.downstream(artefact)
            if self.federation.rules_into(artefact) and artefact not in targets:
                targets.insert(0, artefact)
            for target in targets:
                if target in affected and target not in commands:
                    command = self.command(view, target, after, event["plan"], event["causation"])
                    if command:
                        commands[target] = command
        return list(commands.values())

    # -- proposing ----------------------------------------------------------------------

    def auto(self, record: Dict[str, Any]) -> bool:
        return record["policy"] == AUTOMATIC or (record["policy"] == SEMI_AUTOMATIC and not record["structural"])

    def propose(self, record: Dict[str, Any], state: Any) -> PullRequest:
        branch = f"{BRANCH_PREFIX}{record['plan']}/{record['id']}"
        tmp = Path(tempfile.mkdtemp(prefix="femm-cmd-"))
        work = tmp / "w"
        try:
            self.git.run("worktree", "add", "-q", "-b", branch, str(work), record["base_commit"])
            self.module.file_stores(work / self.root)[record["target"]].write(state)
            log = work / LOG_DIR / record["plan"] / f"{record['id']}.json"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(json.dumps(record, indent=2) + "\n")
            self.git.run("add", "-A", cwd=work)
            self.git.run("commit", "-q", "-m", self.title(record), "-m", f"Femm-Command: {record['id']}",
                         cwd=work, env=BOT)
        finally:
            self.git.run("worktree", "remove", "--force", str(work))
            shutil.rmtree(tmp, ignore_errors=True)
        self.forge.push(branch)
        return self.forge.open_pr(branch, self.title(record), self.body(record))

    @staticmethod
    def title(record: Dict[str, Any]) -> str:
        return f"femm: update {record['target']} ({record['plan']})"

    @staticmethod
    def body(record: Dict[str, Any]) -> str:
        decision = ("merged automatically (policy: automatic)" if record["policy"] == AUTOMATIC else
                    "merged automatically (semi-automatic policy, value update)"
                    if record["policy"] == SEMI_AUTOMATIC and not record["structural"] else
                    f"**needs approval by the owner ({record['owner']})**: merge to approve, close to reject")
        rows = "\n".join(f"| `{d['ref']}` | {d['kind']} | `{json.dumps(d['old'])}` | `{json.dumps(d['new'])}` |"
                         for d in record["deltas"])
        return (f"Command `{record['id']}` of plan `{record['plan']}`, caused by `{record['causation']}`.\n\n"
                f"* target: `{record['path']}` (owner: {record['owner']}, policy: {record['policy']})\n"
                f"* computed against blob `{record['base_blob']}` of commit `{record['base_commit'][:8]}`\n"
                f"* structural: {record['structural']}\n* decision: {decision}\n\n"
                f"| element | change | old | new |\n|---|---|---|---|\n{rows}\n\n"
                f"If `{record['path']}` changes on main before this is merged, the planner closes this pull "
                f"request as a conflict and proposes the step again.")

    # -- one planner run ----------------------------------------------------------------------

    def stale(self, records: List[Dict[str, Any]], head: str) -> List[Dict[str, Any]]:
        return [r for r in records if self.git.blob(head, r["path"]) != r["base_blob"]]

    def on_push(self, before: str, after: str, max_rounds: int = 25) -> Dict[str, Any]:
        """
        Handle the changes before..after on main; continue while commands are merged automatically.

        If the planner has run before, it continues from the last commit it handled, so a
        skipped or cancelled run (e.g. dropped by the workflow concurrency group) loses no events.
        """
        cursor = self.forge.cursor()
        if cursor and cursor != after and self._ancestor(cursor, after):
            before = cursor
        elif cursor == after:
            before = after
        if not before or before == ZERO:
            before = self.git.sha(f"{after}~1")
        proposed, merged, closed = [], [], []
        for _ in range(max_rounds):
            commands = {r["target"]: (r, s) for r, s in self.plan(before, after)}
            view = None
            for pr in self.forge.list_open():
                records = self.pr_records(pr)
                stale = self.stale(records, after)
                superseded = [r for r in records if r["target"] in commands]
                if not stale and not superseded:
                    continue
                reason = ("conflict: the target changed on main since the command was computed"
                          if stale else "superseded by a newer command for the same target")
                self.forge.close(pr, f"Femm: {reason}.")
                closed.append({"pr": pr.id, "reason": reason})
                self.report.append(f"close   #{pr.id} {pr.branch}: {reason}")
                for record in stale:
                    if record["target"] in commands:
                        continue
                    view = view or self.view_at(after)
                    replanned = self.command(view, record["target"], after, record["plan"],
                                             f"conflict:{record['id']}", suffix="-r")
                    if replanned:
                        commands[record["target"]] = replanned
            auto_merged = False
            for record, state in commands.values():
                pr = self.propose(record, state)
                proposed.append({"pr": pr.id, "command": record["id"], "target": record["target"]})
                if self.auto(record):
                    self.forge.merge(pr)
                    merged.append(pr.id)
                    auto_merged = True
                    self.report.append(f"merge   #{pr.id} {record['target']} ({record['policy']})")
                else:
                    self.report.append(f"propose #{pr.id} {record['target']}: waits for {record['owner']}")
            if not auto_merged:
                break
            before, after = after, self.forge.head()
        self.forge.set_cursor(after)
        baseline = self.baseline(after)
        return {"head": after, "proposed": proposed, "merged": merged, "closed": closed,
                "open": [pr.id for pr in self.forge.list_open()], "baseline": baseline, "log": self.report}

    def _ancestor(self, a: str, b: str) -> bool:
        try:
            self.git.run("merge-base", "--is-ancestor", a, b)
            return True
        except RuntimeError:
            return False

    def baseline(self, head: str) -> Optional[str]:
        if self.forge.list_open():
            return None
        violations = self.federation.violations(self.view_at(head))
        if violations:
            self.report.append(f"no baseline: violated rules into {violations}")
            return None
        tags = self.git.run("tag", "--list", "femm-baseline-*").split()
        if any(self.git.sha(f"{t}^{{commit}}") == head for t in tags):
            return None
        numbers = [int(m.group(1)) for t in tags if (m := re.fullmatch(r"femm-baseline-(\d+)", t))]
        tag = f"femm-baseline-{max(numbers, default=0) + 1}"
        self.git.run("tag", "-a", tag, head, "-m", f"Femm baseline: consistent federation at {head[:8]}", env=BOT)
        self.forge.push_tag(tag)
        self.report.append(f"baseline {tag} at {head[:8]}")
        return tag

    def check_pr(self, head: str) -> List[Dict[str, Any]]:
        """Stale command records of a pull request head (for a required status check)."""
        main = self.forge.head()
        base = self.git.run("merge-base", main, head)
        records = [json.loads(self.git.show(head, p)) for p in self.git.added(base, head, LOG_DIR)]
        return self.stale(records, main)

    def codeowners(self) -> str:
        owners = self.config.get("owners", {})
        lines = ["# Generated by: python3 -m femm git codeowners"]
        for name, spec in self.federation.artefacts.items():
            handles = " ".join(owners.get(spec.owner, [])) or f"# {spec.owner}: no handle configured"
            lines.append(f"/{self.path(name)} {handles}")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m femm git")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--forge", choices=["github", "local"], default="github")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("on-push", help="plan and propose commands for a push to main")
    p.add_argument("--before", default="")
    p.add_argument("--after", required=True)
    p.add_argument("--summary", help="append a Markdown summary to this file")
    p = sub.add_parser("plan", help="show the commands for before..after without proposing them")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p = sub.add_parser("check-pr", help="fail if a command pull request is based on an outdated version")
    p.add_argument("--head", required=True)
    sub.add_parser("codeowners", help="print CODEOWNERS lines for the federation")
    args = parser.parse_args(argv)

    git = Git(Path(args.repo))
    config = load_config(Path(args.repo))
    forge = GitHubForge(git, main=config["main"]) if args.forge == "github" else LocalForge(git, config["main"])
    planner = GitPlanner(Path(args.repo), forge, config)

    if args.command == "codeowners":
        print(planner.codeowners(), end="")
        return 0
    if args.command == "plan":
        for record, _ in planner.plan(git.sha(args.before), git.sha(args.after)):
            print(json.dumps(record, indent=2))
        print("\n".join(planner.report), file=sys.stderr)
        return 0
    if args.command == "check-pr":
        stale = planner.check_pr(git.sha(args.head))
        for record in stale:
            print(f"conflict: {record['path']} changed on main since command {record['id']} was computed "
                  f"(base blob {record['base_blob'][:8]})")
        return 1 if stale else 0
    result = planner.on_push(args.before and git.sha(args.before), git.sha(args.after))
    print("\n".join(result["log"]))
    if args.summary:
        with open(args.summary, "a") as f:
            f.write("## Femm planner\n\n```\n" + "\n".join(result["log"]) + "\n```\n\n"
                    f"open command pull requests: {result['open'] or 'none'}; "
                    f"baseline: {result['baseline'] or 'none'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
