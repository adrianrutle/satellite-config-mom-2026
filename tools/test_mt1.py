"""
Tests for MT.1 Change Impact & Reconciliation module.

Most tests copy a real snapshot (v1/ or v2/) to a temporary directory, edit one
source artefact and check that the change reaches every dependent artefact.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import artefacts as A
from mt1_change_impact import (
    Change,
    ChangeDetector,
    ChangeKind,
    GranularityLevel,
    ImpactAnalyzer,
    PropagationEngine,
    PropagationStrategy,
)
from mt1_orchestrate import main as cli

REPO = Path(__file__).resolve().parent.parent
V1, V2 = REPO / "v1", REPO / "v2"


class SnapshotTestCase(unittest.TestCase):
    """Provides a writable copy of a snapshot."""

    def copy_snapshot(self, source: Path) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        target = tmp / source.name
        shutil.copytree(source, target)
        return target

    @staticmethod
    def edit(path: Path, old: str, new: str):
        text = path.read_text()
        assert old in text, f"{old!r} not found in {path}"
        path.write_text(text.replace(old, new, 1))

    @staticmethod
    def set_thruster_mass(snapshot: Path, old: str, new: str):
        """Edit the thruster's mass value individual, as the manufacturer would."""
        path = snapshot / A.CATALOGUE
        text = path.read_text()
        start = text.index('rdf:about="http://www.example.com/satellite#MainThruster001_mass_value"')
        pos = text.index(f">{old}</vim4:hasDoubleNumber>", start)
        path.write_text(text[:pos] + f">{new}" + text[pos + len(old) + 1:])

    def propagate(self, snapshot: Path, strategy=PropagationStrategy.AUTOMATIC, baseline: Path = None):
        snap = A.load_snapshot(snapshot)
        engine = PropagationEngine(strategy)
        result = engine.reconcile(snap, A.load_snapshot(baseline) if baseline else None)
        engine.apply(snap, result)
        return result


class TestArtefacts(unittest.TestCase):
    """Loaders and renderers reproduce the reference artefacts exactly."""

    def test_catalogue_masses_follow_vim4_chain(self):
        catalogue = A.load_catalogue(V1 / A.CATALOGUE)
        self.assertEqual(catalogue["T001"].mass_kg, 85.0)
        self.assertEqual(catalogue["T001"].part_class, "Thruster")
        self.assertEqual(A.load_catalogue(V2 / A.CATALOGUE)["T001"].mass_kg, 95.0)
        self.assertEqual(len(catalogue), 6)

    def test_renderers_reproduce_reference_files(self):
        for version in (V1, V2):
            snap = A.load_snapshot(version)
            self.assertEqual(A.render_bom_xml(snap.bom), snap.bom_xml_text, version)
            self.assertEqual(A.render_network_xml(snap.network), snap.network_xml_text, version)
            self.assertEqual(A.render_report(snap.requirements, snap.bom), snap.report_text, version)
            self.assertEqual(A.dump_bom_json(snap.bom), (version / A.BOM_JSON).read_text(), version)

    def test_constraint_operators(self):
        self.assertTrue(A.evaluate_constraint(100.0, "<=", 150.0))
        self.assertFalse(A.evaluate_constraint(200.0, "<=", 150.0))
        self.assertTrue(A.evaluate_constraint(100.0, ">=", 50.0))
        self.assertFalse(A.evaluate_constraint(100.0, ">=", 150.0))
        self.assertFalse(A.evaluate_constraint(100.0, "~", 150.0))


class TestRepositoryConsistency(unittest.TestCase):
    """The committed snapshots must already be reconciled."""

    def test_snapshots_are_consistent(self):
        for version in (V1, V2):
            result = PropagationEngine(PropagationStrategy.MANUAL).reconcile(A.load_snapshot(version))
            self.assertTrue(result.consistent, [u.to_dict() for u in result.updates] + result.errors)

    def test_expected_verification_results(self):
        v1 = PropagationEngine().reconcile(A.load_snapshot(V1))
        v2 = PropagationEngine().reconcile(A.load_snapshot(V2))
        self.assertEqual(v1.bom["calculated_total_mass_kg"], 322.0)
        self.assertTrue(v1.all_satisfied)
        self.assertEqual(v2.bom["calculated_total_mass_kg"], 357.0)
        self.assertFalse(v2.all_satisfied)


class TestChangeDetector(unittest.TestCase):

    def test_change_hash_consistency(self):
        a = Change("catalogue.owl", "T001", "mass", 85.0, 95.0)
        b = Change("catalogue.owl", "T001", "mass", 85.0, 95.0)
        self.assertEqual(a.change_hash, b.change_hash)
        self.assertEqual(len(a.change_hash), 8)

    def test_detects_v1_to_v2_changes(self):
        changes = ChangeDetector().detect(A.load_snapshot(V1), A.load_snapshot(V2))
        nodes = {c.node: c for c in changes}
        self.assertEqual(nodes["catalogue.owl:T001:mass"].old_value, 85.0)
        self.assertEqual(nodes["catalogue.owl:T001:mass"].new_value, 95.0)
        self.assertEqual(nodes["bom.json:ANT002"].kind, ChangeKind.ADDED)
        self.assertEqual(nodes["comm-network.json:AntennaBackup"].kind, ChangeKind.ADDED)
        self.assertEqual(nodes["comm-network.json:AntennaPrimary:component_id"].old_value, "Antenna")
        self.assertNotIn("requirements.csv:REQ001:Value", nodes)


class TestImpactAnalyzer(unittest.TestCase):

    def setUp(self):
        self.analyzer = ImpactAnalyzer(A.load_snapshot(V2))

    def test_catalogue_change_reaches_report(self):
        change = Change("catalogue.owl", "T001", "mass", 85.0, 95.0)
        graph = self.analyzer.analyze([change])
        self.assertIn([
            "catalogue.owl:T001:mass",
            "bom.json:T001:mass_kg_per_unit",
            "bom.json:total_system:calculated_total_mass_kg",
            "report.md:REQ001:status",
        ], graph.paths_from(change.node))
        self.assertIn("bom.xml", graph.nodes)

    def test_network_node_reaches_bom_quantity(self):
        node = {"component_id": "AntennaBackup", "type": "ANT002", "role": "backup_transceiver"}
        graph = self.analyzer.analyze([Change("comm-network.json", "AntennaBackup", "", None, node, ChangeKind.ADDED)])
        self.assertIn("bom.json:ANT002:quantity", graph.nodes)
        self.assertIn("report.md:REQ001:status", graph.nodes)

    def test_link_change_does_not_touch_bom(self):
        link = {"source": "Controller", "target": "AntennaBackup", "type": "rf_connection", "protocol": "X-band"}
        graph = self.analyzer.analyze([Change("comm-network.json", "Controller->AntennaBackup", "", None, link,
                                              ChangeKind.ADDED)])
        self.assertEqual(set(graph.nodes), {"comm-network.json:Controller->AntennaBackup", "comm-network.xml"})

    def test_graphs_compose_and_coarsen(self):
        a = Change("catalogue.owl", "T001", "mass", 85.0, 95.0)
        b = Change("requirements.csv", "REQ001", "Value", "350", "340")
        graph = self.analyzer.analyze([a, b])
        self.assertEqual(graph.nodes["report.md:REQ001:status"], {a.change_hash, b.change_hash})
        model = graph.coarsened(GranularityLevel.MODEL)
        self.assertIn(("bom.json", "report.md", "requirement_verification"), model.edges)


class TestPropagation(SnapshotTestCase):

    def test_catalogue_change_propagates_to_bom_xml_and_report(self):
        snapshot = self.copy_snapshot(V1)
        self.set_thruster_mass(snapshot, "85.0", "95.0")

        result = self.propagate(snapshot)

        bom = json.loads((snapshot / A.BOM_JSON).read_text())
        t001 = next(c for c in bom["components"] if c["type"] == "T001")
        self.assertEqual(t001["mass_kg_per_unit"], 95.0)
        self.assertEqual(bom["calculated_total_mass_kg"], 342.0)
        self.assertIn("<mass_kg_per_unit>95</mass_kg_per_unit>", (snapshot / A.BOM_XML).read_text())
        self.assertIn("| 342.0 | kg | SATISFIED |", (snapshot / A.REPORT).read_text())
        self.assertTrue(result.all_satisfied)

    def test_network_change_adds_bom_entry_and_violates_requirement(self):
        snapshot = self.copy_snapshot(V1)
        self.set_thruster_mass(snapshot, "85.0", "95.0")
        network = json.loads((snapshot / A.NETWORK_JSON).read_text())
        network["nodes"].append({"component_id": "AntennaBackup", "type": "ANT002", "role": "backup_transceiver"})
        (snapshot / A.NETWORK_JSON).write_text(json.dumps(network, indent=2) + "\n")

        result = self.propagate(snapshot)

        bom = json.loads((snapshot / A.BOM_JSON).read_text())
        self.assertIn({"type": "ANT002", "quantity": 1, "mass_kg_per_unit": 15.0}, bom["components"])
        self.assertEqual(bom["calculated_total_mass_kg"], 357.0)
        self.assertIn("NOT SATISFIED", (snapshot / A.REPORT).read_text())
        self.assertIn("AntennaBackup", (snapshot / A.NETWORK_XML).read_text())
        self.assertFalse(result.all_satisfied)
        # Reconciled snapshot is now stable.
        self.assertTrue(self.propagate(snapshot).consistent)

    def test_requirement_change_regenerates_report(self):
        snapshot = self.copy_snapshot(V2)
        self.edit(snapshot / A.REQUIREMENTS, '"<=",350', '"<=",400')
        self.propagate(snapshot)
        self.assertIn("| <= 400 | 357.0 | kg | SATISFIED |", (snapshot / A.REPORT).read_text())

    def test_removed_comms_type_needs_baseline(self):
        snapshot = self.copy_snapshot(V2)
        network = json.loads((snapshot / A.NETWORK_JSON).read_text())
        network["nodes"] = [n for n in network["nodes"] if n["type"] != "ANT002"]
        network["links"] = [l for l in network["links"] if l["target"] != "AntennaBackup"]
        (snapshot / A.NETWORK_JSON).write_text(json.dumps(network, indent=2) + "\n")

        self.propagate(snapshot, baseline=V2)

        bom = json.loads((snapshot / A.BOM_JSON).read_text())
        self.assertNotIn("ANT002", [c["type"] for c in bom["components"]])
        self.assertEqual(bom["calculated_total_mass_kg"], 342.0)

    def test_semi_automatic_leaves_structural_updates_pending(self):
        snapshot = self.copy_snapshot(V1)
        network = json.loads((snapshot / A.NETWORK_JSON).read_text())
        network["nodes"].append({"component_id": "AntennaBackup", "type": "ANT002", "role": "backup_transceiver"})
        (snapshot / A.NETWORK_JSON).write_text(json.dumps(network, indent=2) + "\n")

        result = self.propagate(snapshot, PropagationStrategy.SEMI_AUTOMATIC)

        self.assertEqual([(u.element, u.structural) for u in result.pending], [("ANT002", True)])
        bom = json.loads((snapshot / A.BOM_JSON).read_text())
        self.assertNotIn("ANT002", [c["type"] for c in bom["components"]])
        self.assertIn("AntennaBackup", (snapshot / A.NETWORK_XML).read_text())

    def test_manual_changes_nothing(self):
        snapshot = self.copy_snapshot(V1)
        self.set_thruster_mass(snapshot, "85.0", "95.0")
        before = (snapshot / A.BOM_JSON).read_text()
        result = self.propagate(snapshot, PropagationStrategy.MANUAL)
        self.assertEqual((snapshot / A.BOM_JSON).read_text(), before)
        self.assertFalse(result.consistent)
        self.assertTrue(all(not u.applied for u in result.updates))

    def test_duplicate_bom_entries_are_merged(self):
        snapshot = self.copy_snapshot(V2)
        bom = json.loads((snapshot / A.BOM_JSON).read_text())
        bom["components"].append({"type": "ANT002", "quantity": 1, "mass_kg_per_unit": 1500.0})
        (snapshot / A.BOM_JSON).write_text(json.dumps(bom, indent=2) + "\n")
        self.propagate(snapshot)
        self.assertEqual((snapshot / A.BOM_JSON).read_text(), (V2 / A.BOM_JSON).read_text())


class TestCommandLine(SnapshotTestCase):

    def test_check_passes_on_repository(self):
        self.assertEqual(cli(["check", str(V1), str(V2)]), 0)

    def test_check_fails_on_stale_snapshot_and_propagate_fixes_it(self):
        snapshot = self.copy_snapshot(V1)
        self.set_thruster_mass(snapshot, "85.0", "90.0")
        self.assertEqual(cli(["check", str(snapshot)]), 1)
        self.assertEqual(cli(["propagate", str(snapshot), "--strategy", "automatic"]), 0)
        self.assertEqual(cli(["check", str(snapshot)]), 0)
        self.assertEqual(cli(["check", str(snapshot), "--fail-on-violation"]), 0)


if __name__ == "__main__":
    unittest.main()
