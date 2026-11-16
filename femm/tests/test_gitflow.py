"""The Git realisation on a temporary repository (pull requests simulated by LocalForge)."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from femm import satellite as S
from femm.gitflow import BOT, Git, GitPlanner, LocalForge

REPO = Path(__file__).resolve().parents[2]
ROOT = "fed"
HUMAN = {"GIT_AUTHOR_NAME": "Engineer", "GIT_AUTHOR_EMAIL": "engineer@example.com",
         "GIT_COMMITTER_NAME": "Engineer", "GIT_COMMITTER_EMAIL": "engineer@example.com"}


@unittest.skipIf(shutil.which("git") is None, "git not available")
class GitRealisation(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        shutil.copytree(REPO / "v1", self.repo / ROOT)
        config = {"module": "femm.satellite", "root": ROOT, "main": "main",
                  "owners": {"systems_engineer": ["@systems"], "manufacturer": ["@manufacturer"]}}
        (self.repo / "femm.json").write_text(json.dumps(config))
        self.git = Git(self.repo)
        self.git.run("init", "-q", "-b", "main")
        self.git.run("add", "-A")
        self.git.run("commit", "-q", "-m", "v1", env=HUMAN)
        self.forge = LocalForge(self.git)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def planner(self):
        return GitPlanner(self.repo, self.forge)

    def commit_files(self, message, *rels, source=REPO / "v2"):
        before = self.git.sha("main")
        for rel in rels:
            shutil.copy(source / rel, self.repo / ROOT / rel)
        self.git.run("commit", "-q", "-am", message, env=HUMAN)
        return before, self.git.sha("main")

    def file(self, name):
        return (self.repo / ROOT / S.PATHS[name]).read_text()

    def v2(self, name):
        return (REPO / "v2" / S.PATHS[name]).read_text()

    def test_es1_value_update_is_merged_automatically(self):
        before, after = self.commit_files("manufacturer: T001 95 kg", S.PATHS[S.CATALOGUE])
        result = self.planner().on_push(before, after)
        self.assertEqual(result["open"], [])
        self.assertEqual(len(result["merged"]), 3)                  # bom, then bom.xml and report
        self.assertIn('"calculated_total_mass_kg": 342.0', self.file(S.BOM))
        self.assertIn("| 342.0 | kg | SATISFIED |", self.file(S.REPORT))
        self.assertEqual(result["baseline"], "femm-baseline-1")
        log = sorted((self.repo / ".femm" / "log").rglob("*.json"))
        self.assertEqual(len(log), 3)
        plans = {json.loads(p.read_text())["plan"] for p in log}
        self.assertEqual(len(plans), 1)                             # one plan, correlated across pushes (A4)

    def test_es1_es2_with_owner_approval_reaches_v2(self):
        before, after = self.commit_files("manufacturer: T001 95 kg", S.PATHS[S.CATALOGUE])
        self.planner().on_push(before, after)
        before, after = self.commit_files("comms: backup antenna", S.PATHS[S.NETWORK])
        result = self.planner().on_push(before, after)
        self.assertEqual(len(result["open"]), 1)                    # structural BoM change waits
        self.assertIsNone(result["baseline"])
        self.assertEqual(self.file(S.NETWORK_XML), self.v2(S.NETWORK_XML))
        pr = self.forge.list_open()[0]
        self.assertIn("bom", pr.branch)
        before = self.git.sha("main")
        after = self.forge.merge(pr)                                # the systems engineer approves
        result = self.planner().on_push(before, after)
        for name in (S.BOM, S.BOM_XML, S.NETWORK_XML, S.REPORT):
            self.assertEqual(self.file(name), self.v2(name), name)
        self.assertEqual(result["baseline"], "femm-baseline-2")

    def test_rejection_by_closing_leaves_visible_inconsistency(self):
        before, after = self.commit_files("comms: backup antenna", S.PATHS[S.NETWORK])
        self.planner().on_push(before, after)
        pr = self.forge.list_open()[0]
        self.forge.close(pr, "not agreed")
        planner = self.planner()
        self.assertIsNone(planner.baseline(self.git.sha("main")))
        self.assertIn("violated rules into ['bom']", planner.report[-1])

    def test_local_edit_while_command_waits_is_not_lost(self):
        before, after = self.commit_files("comms: backup antenna", S.PATHS[S.NETWORK])
        self.planner().on_push(before, after)
        waiting = self.forge.list_open()[0]
        self.assertEqual(self.planner().check_pr(self.git.sha(waiting.branch)), [])
        # The systems engineer edits the BoM directly: two attitude controllers.
        bom = json.loads(self.file(S.BOM))
        next(c for c in bom["components"] if c["type"] == "AC001")["quantity"] = 2
        bom["calculated_total_mass_kg"] += 12.0
        (self.repo / ROOT / S.PATHS[S.BOM]).write_text(json.dumps(bom, indent=2) + "\n")
        before = self.git.sha("main")
        self.git.run("commit", "-q", "-am", "systems: second attitude controller", env=HUMAN)
        after = self.git.sha("main")
        self.assertEqual(len(self.planner().check_pr(self.git.sha(waiting.branch))), 1)   # now stale
        result = self.planner().on_push(before, after)
        self.assertEqual([c["pr"] for c in result["closed"]], [waiting.id])
        self.assertEqual(len(result["open"]), 1)
        after = self.forge.merge(self.forge.list_open()[0])
        self.planner().on_push(self.git.sha("main~1"), after)
        bom = json.loads(self.file(S.BOM))
        quantities = {c["type"]: c["quantity"] for c in bom["components"]}
        self.assertEqual(quantities["AC001"], 2)                    # the local edit survived
        self.assertEqual(quantities["ANT002"], 1)                   # and the command was applied
        self.assertEqual(bom["calculated_total_mass_kg"], 334.0 + 15.0)
        self.assertEqual(S.federation().violations(S.load_view(self.repo / ROOT)), [])

    def test_skipped_run_loses_no_events(self):
        before, after = self.commit_files("manufacturer: T001 95 kg", S.PATHS[S.CATALOGUE])
        self.planner().on_push(before, after)                       # sets refs/femm/processed
        self.commit_files("comms: backup antenna", S.PATHS[S.NETWORK])   # its planner run is dropped
        (self.repo / "NOTES.md").write_text("unrelated\n")
        self.git.run("add", "-A")
        before = self.git.sha("main")
        self.git.run("commit", "-q", "-m", "notes", env=HUMAN)
        result = self.planner().on_push(before, self.git.sha("main"))
        self.assertEqual(self.file(S.NETWORK_XML), self.v2(S.NETWORK_XML))
        self.assertEqual(len(result["open"]), 1)                    # the BoM command for ANT002

    def test_codeowners(self):
        text = self.planner().codeowners()
        self.assertIn(f"/{ROOT}/systems_engineer/bom.json @systems", text)
        self.assertIn(f"/{ROOT}/manufacturer/catalogue.owl @manufacturer", text)


if __name__ == "__main__":
    unittest.main()
