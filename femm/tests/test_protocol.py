"""
Protocol properties on randomised schedules (the Maude analyses of the paper, as tests).

The federation is the instance analysed in Maude: catalogue -> bom -> report,
where the BoM has a unit mass that corresponds to the catalogue mass and a
locally owned mass. Each run delivers messages in a random order, duplicates
some of them, and interleaves two local edits (ES.1 and ES.2) and the owners'
decisions at random points.

    P2 Termination      every run reaches a quiescent state
    P3 Convergence      the final state is consistent (unless a command was rejected)
    P4 No lost updates  no command is applied to a version it was not computed against
"""

import random
import unittest

from femm import AUTOMATIC, MANUAL, ArtefactSpec, Federation, FederationError, Rule, System
from femm import messages as M
from femm.bus import InMemoryBus
from femm.federation import CORRESPONDENCE, DERIVATION, json_diff

RUNS = 300


def toy(policy=AUTOMATIC):
    def bom_rule(view, bom):
        return {"unit": view["cat"]["mass"], "other": bom["other"]}

    def report_rule(view, _):
        return {"ok": 2 * view["bom"]["unit"] + view["bom"]["other"] <= 350}

    return Federation(
        [ArtefactSpec("cat", "manufacturer"), ArtefactSpec("bom", "systems", policy), ArtefactSpec("rep", "verification")],
        [Rule("mass", CORRESPONDENCE, ("cat",), "bom", bom_rule),
         Rule("status", CORRESPONDENCE, ("bom",), "rep", report_rule)])


INITIAL = {"cat": {"mass": 85}, "bom": {"unit": 85, "other": 152}, "rep": {"ok": True}}
EDITS = [("cat", lambda s: {"mass": s["mass"] + 10}),               # ES.1
         ("bom", lambda s: {"unit": s["unit"], "other": s["other"] + 15})]   # ES.2


def random_run(seed, policy=AUTOMATIC, duplicates=2, check=True, approve_probability=0.8):
    rng = random.Random(seed)
    bus = InMemoryBus(seed, shuffle=True, duplicates=duplicates)
    system = System(toy(policy), INITIAL, bus, check_base_version=check)
    edits = list(EDITS)
    rng.shuffle(edits)
    for _ in range(10_000):
        choices = []
        if not bus.idle:
            choices += ["deliver"] * 3
        if edits:
            choices.append("edit")
        if system.awaiting():
            choices.append("decide")
        if not choices:
            return system
        choice = rng.choice(choices)
        if choice == "deliver":
            bus.step()
        elif choice == "edit":
            artefact, edit = edits.pop()
            system.edit(artefact, edit(system.msas[artefact].state))
        else:
            name = rng.choice(sorted(system.awaiting()))
            if rng.random() < approve_probability:
                system.approve(name)
            else:
                system.reject(name, "random rejection")
    raise AssertionError(f"seed {seed}: no quiescent state after 10000 steps")


class ProtocolProperties(unittest.TestCase):

    def test_termination_and_convergence_automatic(self):
        for seed in range(RUNS):
            system = random_run(seed)
            self.assertTrue(system.quiescent, seed)
            self.assertEqual(system.violations(), [], f"seed {seed}")
            self.assertEqual(system.view()["bom"], {"unit": 95, "other": 167}, seed)
            self.assertEqual(system.view()["rep"], {"ok": False}, seed)

    def test_convergence_manual_unless_rejected(self):
        rejected_runs = 0
        for seed in range(RUNS):
            system = random_run(seed, MANUAL)
            self.assertTrue(system.quiescent, seed)
            if system.rejections == 0:
                self.assertEqual(system.violations(), [], f"seed {seed}")
            else:
                rejected_runs += 1
        self.assertGreater(rejected_runs, 0)   # the schedules do exercise rejection

    def test_no_lost_updates(self):
        for policy in (AUTOMATIC, MANUAL):
            for seed in range(RUNS):
                self.assertEqual(random_run(seed, policy).lost_updates, 0, f"{policy} seed {seed}")

    def test_without_base_version_check_updates_are_lost(self):
        lost = [seed for seed in range(RUNS) if random_run(seed, MANUAL, check=False).lost_updates > 0]
        self.assertTrue(lost, "expected a counterexample without the base-version check")
        system = random_run(lost[0], MANUAL, check=False)
        self.assertEqual(system.lost_updates, 1)

    def test_conflicts_occur_and_are_resolved(self):
        conflicts = 0
        for seed in range(RUNS):
            system = random_run(seed, MANUAL, approve_probability=1.0)
            conflicts += len(system.messages(M.CONFLICT))
            self.assertEqual(system.violations(), [], seed)
        self.assertGreater(conflicts, 0)

    def test_planner_converges_even_without_service_deduplication(self):
        # Regression for the flaw found with Maude: a planner that advanced its view on
        # conflicts lost updates when a conflict was delivered twice. The planner must
        # not depend on A1 for this.
        from femm.services import Service
        Service.deduplicate = False
        try:
            for seed in range(RUNS):
                for policy in (AUTOMATIC, MANUAL):
                    system = random_run(seed, policy, duplicates=4, approve_probability=1.0)
                    self.assertEqual(system.violations(), [], f"{policy} seed {seed}")
        finally:
            Service.deduplicate = True

    def test_every_plan_completes(self):
        for seed in range(RUNS):
            system = random_run(seed, MANUAL)
            started = {m.correlation_id for m in system.messages(M.PLAN_STARTED)}
            completed = {m.correlation_id for m in system.messages(M.PLAN_COMPLETED)}
            self.assertEqual(started, completed, seed)

    def test_baseline_after_convergence(self):
        for seed in range(50):
            system = random_run(seed)
            last = system.versions.baselines[-1]["versions"]
            self.assertEqual(last, {n: m.version for n, m in system.msas.items()}, seed)

    def test_authority_only_owners_write(self):
        system = random_run(7)
        writers = {(m.source, m.artefact) for m in system.messages(M.CHANGED)}
        self.assertTrue(all(source == artefact for source, artefact in writers))
        commands = {m.source for m in system.messages() if m.type == M.UPDATE}
        self.assertEqual(commands, {"synchronization-planning"})


class FederationChecks(unittest.TestCase):

    def test_cyclic_rule_graph_is_rejected(self):
        with self.assertRaises(FederationError):
            Federation([ArtefactSpec("a", "x"), ArtefactSpec("b", "y")],
                       [Rule("ab", CORRESPONDENCE, ("a",), "b", lambda v, t: t),
                        Rule("ba", CORRESPONDENCE, ("b",), "a", lambda v, t: t)])

    def test_derivation_inside_an_artefact_is_allowed(self):
        f = Federation([ArtefactSpec("a", "x")],
                       [Rule("total", DERIVATION, ("a",), "a", lambda v, t: {**t, "sum": t["x"] + t["y"]})])
        self.assertEqual(f.violations({"a": {"x": 1, "y": 2, "sum": 0}}), ["a"])
        self.assertEqual(f.violations({"a": {"x": 1, "y": 2, "sum": 3}}), [])

    def test_json_diff_keyed_lists(self):
        old = {"nodes": [{"id": "n1", "t": "A"}, {"id": "n2", "t": "B"}]}
        new = {"nodes": [{"id": "n2", "t": "C"}, {"id": "n3", "t": "A"}]}
        self.assertEqual(sorted((d["ref"], d["kind"]) for d in json_diff("net", old, new)),
                         [("net:nodes:n1", "removed"), ("net:nodes:n2:t", "modified"), ("net:nodes:n3", "added")])

    def test_duplicate_command_is_ignored(self):
        system = System(toy(MANUAL), INITIAL)
        system.edit("cat", {"mass": 95})
        system.run()
        command = system.awaiting()["bom"]
        system.bus.publish(M.command_topic("bom"), command)      # redelivery
        system.run()
        self.assertEqual(len(system.msas["bom"].queue), 0)


if __name__ == "__main__":
    unittest.main()
