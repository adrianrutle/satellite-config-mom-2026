"""The engineering scenarios ES.1-ES.3 of the challenge on the satellite federation."""

import copy
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from femm import AUTOMATIC, MANUAL, System
from femm import messages as M
from femm import satellite as S
from femm.bus import InMemoryBus

REPO = Path(__file__).resolve().parents[2]
V1, V2 = S.load_view(REPO / "v1"), S.load_view(REPO / "v2")


def es3_alternative(network):
    """The second engineer of ES.3: deletes ANT001 and adds two ANT002 nodes."""
    network = copy.deepcopy(network)
    network["nodes"] = [n for n in network["nodes"] if n["type"] != "ANT001"] + [
        {"component_id": "AntennaB1", "type": "ANT002", "role": "transceiver"},
        {"component_id": "AntennaB2", "type": "ANT002", "role": "transceiver"}]
    network["links"] = [{"source": "Controller", "target": t, "type": "rf_connection", "protocol": "X-band"}
                        for t in ("AntennaB1", "AntennaB2")]
    return network


class Scenarios(unittest.TestCase):

    def test_reference_versions_are_baselines(self):
        federation = S.federation()
        self.assertEqual(federation.violations(V1), [])
        self.assertEqual(federation.violations(V2), [])

    def test_es1_thruster_mass(self):
        system = System(S.federation(), V1)
        system.edit(S.CATALOGUE, V2[S.CATALOGUE], author="manufacturer")
        system.run()
        view = system.view()
        self.assertEqual(view[S.BOM]["calculated_total_mass_kg"], 342.0)
        self.assertIn("| 342.0 | kg | SATISFIED |", view[S.REPORT])
        self.assertEqual(system.violations(), [])
        self.assertEqual(system.awaiting(), {})        # value updates pass the semi-automatic policy
        # Impact chain of Section 3.6
        graph = system.impact.graphs[system.messages(M.CHANGED)[0].id]
        for edge in [("catalogue:T001:mass", "bom:T001:mass_kg_per_unit"),
                     ("bom:T001:mass_kg_per_unit", "bom:total_system:calculated_total_mass_kg"),
                     ("bom:total_system:calculated_total_mass_kg", "report:REQ001:status")]:
            self.assertIn(edge, {(s, t) for s, t, _ in graph.edges})
        self.assertEqual(len(system.versions.baselines), 2)

    def test_es2_rename_does_not_reach_bom_and_addition_waits_for_approval(self):
        system = System(S.federation(), V1)
        system.edit(S.CATALOGUE, V2[S.CATALOGUE])
        system.run()
        event = system.edit(S.NETWORK, V2[S.NETWORK], author="communications engineer")
        refs = {d["ref"]: d["kind"] for d in event.data["deltas"]}
        self.assertEqual(refs.get("network:AntennaPrimary:component_id"), "modified")   # a rename
        self.assertEqual(refs.get("network:AntennaBackup"), "added")
        system.run()
        graph = system.impact.graphs[event.id]
        bom_refs = {n for n in graph.nodes if n.startswith("bom:")}
        self.assertIn("bom:ANT002:quantity", bom_refs)
        self.assertNotIn("bom:ANT001:quantity", bom_refs)
        waiting = system.awaiting()
        self.assertEqual(list(waiting), [S.BOM])
        self.assertTrue(waiting[S.BOM].data["structural"])
        system.approve(S.BOM, by="systems engineer")
        system.run()
        self.assertEqual(system.view(), V2)
        self.assertEqual(system.validation.results, {"REQ001": False})

    def test_es2_rejected_by_owner(self):
        system = System(S.federation(), V1)
        system.edit(S.NETWORK, V2[S.NETWORK])
        system.run()
        system.reject(S.BOM, "backup antenna not agreed", by="systems engineer")
        system.run()
        self.assertEqual(system.violations(), [S.BOM])          # visible, not silently fixed
        completed = system.messages(M.PLAN_COMPLETED)[-1]
        self.assertEqual(completed.data["status"], "rejected")

    def test_es3_concurrent_network_edits(self):
        system = System(S.federation(), V1)
        base = system.msas[S.NETWORK].version
        first = system.edit(S.NETWORK, V2[S.NETWORK], base_version=base, author="engineer 1")
        second = system.edit(S.NETWORK, es3_alternative(V1[S.NETWORK]), base_version=base, author="engineer 2")
        self.assertEqual(first.type, M.CHANGED)
        self.assertEqual(second.type, M.CONFLICT)
        self.assertIn("network:AntennaB1", {d["ref"] for d in second.data["deltas"]})
        system.settle()
        self.assertEqual(system.view()[S.NETWORK], V2[S.NETWORK])
        self.assertEqual(system.violations(), [])

    def test_es1_and_es2_under_random_schedules(self):
        for seed in range(60):
            rng = random.Random(seed)
            bus = InMemoryBus(seed, shuffle=True, duplicates=3)
            system = System(S.federation(), V1, bus)
            edits = [(S.CATALOGUE, V2[S.CATALOGUE]), (S.NETWORK, V2[S.NETWORK])]
            rng.shuffle(edits)
            while edits or not system.quiescent:
                if edits and (bus.idle or rng.random() < 0.2):
                    system.edit(*edits.pop())
                elif not bus.idle:
                    bus.step()
                else:
                    system.approve(S.BOM)
            self.assertEqual(system.view(), V2, f"seed {seed}")
            self.assertEqual(system.lost_updates, 0)

    def test_hand_edited_derived_artefact_is_restored(self):
        system = System(S.federation(), V1)
        system.edit(S.REPORT, V1[S.REPORT].replace("| SATISFIED |", "| WAIVED |"), author="someone")
        system.run()
        self.assertEqual(system.view()[S.REPORT], V1[S.REPORT])
        self.assertEqual(system.messages(M.UPDATE)[0].artefact, S.REPORT)

    def test_policies(self):
        for policy, waits in ((AUTOMATIC, False), (MANUAL, True)):
            system = System(S.federation(bom_policy=policy), V1)
            system.edit(S.CATALOGUE, V2[S.CATALOGUE])
            system.run()
            self.assertEqual(S.BOM in system.awaiting(), waits, policy)


class Files(unittest.TestCase):
    """The same scenarios with the artefacts as files in their native formats."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "v"
        shutil.copytree(REPO / "v1", self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_saving_files_propagates_to_v2(self):
        system = System(S.federation(), S.load_view(self.root), stores=S.file_stores(self.root))
        for rel in (S.PATHS[S.CATALOGUE], S.PATHS[S.NETWORK]):      # the teams save their files
            shutil.copy(REPO / "v2" / rel, self.root / rel)
        system.msas[S.CATALOGUE].detect(author="manufacturer")
        system.msas[S.NETWORK].detect(author="communications engineer")
        system.settle()
        for name in (S.BOM, S.BOM_XML, S.NETWORK_XML, S.REPORT):
            rel = S.PATHS[name]
            self.assertEqual((self.root / rel).read_text(), (REPO / "v2" / rel).read_text(), rel)

    def test_source_artefacts_are_never_written(self):
        store = S.FileStore(self.root, S.CATALOGUE)
        with self.assertRaises(PermissionError):
            store.write(store.read())


if __name__ == "__main__":
    unittest.main()
