"""
Tests for MT.1 Change Impact & Reconciliation module.
"""

import unittest
from mt1_change_impact import (
    ChangeDetector,
    ImpactAnalyzer,
    PropagationEngine,
    Change,
    PropagationStrategy,
    GranularityLevel,
)


class TestChangeDetector(unittest.TestCase):
    """Tests for change detection."""
    
    def test_change_object_creation(self):
        """Test Change object creation and hash generation."""
        change = Change(
            source_file="catalogue.owl",
            component_id="T001",
            attribute="mass",
            old_value=85.0,
            new_value=95.0
        )
        
        self.assertEqual(change.component_id, "T001")
        self.assertEqual(change.old_value, 85.0)
        self.assertEqual(change.new_value, 95.0)
        self.assertIsNotNone(change.change_hash)
        self.assertEqual(len(change.change_hash), 8)
    
    def test_change_hash_consistency(self):
        """Test that identical changes produce the same hash."""
        change1 = Change(
            source_file="catalogue.owl",
            component_id="T001",
            attribute="mass",
            old_value=85.0,
            new_value=95.0
        )
        
        change2 = Change(
            source_file="catalogue.owl",
            component_id="T001",
            attribute="mass",
            old_value=85.0,
            new_value=95.0
        )
        
        self.assertEqual(change1.change_hash, change2.change_hash)


class TestImpactAnalyzer(unittest.TestCase):
    """Tests for impact analysis."""
    
    def test_impact_analysis_basic(self):
        """Test basic impact analysis."""
        changes = [
            Change(
                source_file="catalogue.owl",
                component_id="T001",
                attribute="mass",
                old_value=85.0,
                new_value=95.0
            )
        ]
        
        analyzer = ImpactAnalyzer("bom.json", "requirements.csv", "report.md")
        impacted = analyzer.analyze_impact(changes)
        
        self.assertEqual(len(impacted), 3)
        model_files = [elem.model_file for elem in impacted]
        self.assertIn("bom.json", model_files)
        self.assertIn("report.md", model_files)
    
    def test_impact_graph_structure(self):
        """Test impact graph generation."""
        changes = [
            Change(
                source_file="catalogue.owl",
                component_id="T001",
                attribute="mass",
                old_value=85.0,
                new_value=95.0
            )
        ]
        
        analyzer = ImpactAnalyzer("bom.json", "requirements.csv", "report.md")
        analyzer.analyze_impact(changes)
        graph = analyzer.build_impact_structure()
        
        self.assertIn("source", graph)
        self.assertIn("propagation_chain", graph)
        self.assertIn("impacted_elements", graph)
        
        chain = graph["propagation_chain"]
        self.assertGreater(len(chain), 0)
        
        for step in chain:
            self.assertIn("step", step)
            self.assertIn("from", step)
            self.assertIn("to", step)
            self.assertIn("type", step)


class TestPropagationEngine(unittest.TestCase):
    """Tests for change propagation."""
    
    def test_propagation_bom_update(self):
        """Test BOM update propagation."""
        changes = [
            Change(
                source_file="catalogue.owl",
                component_id="T001",
                attribute="mass",
                old_value=85.0,
                new_value=95.0
            )
        ]
        
        bom_data = {
            "components": [
                {"type": "T001", "quantity": 2, "mass_kg_per_unit": 85.0},
                {"type": "SP001", "quantity": 2, "mass_kg_per_unit": 55.0},
                {"type": "ANT001", "quantity": 1, "mass_kg_per_unit": 22.0},
                {"type": "AC001", "quantity": 1, "mass_kg_per_unit": 12.0},
                {"type": "CC001", "quantity": 1, "mass_kg_per_unit": 8.0},
            ],
            "calculated_total_mass_kg": 322.0
        }
        
        requirements_data = [
            {
                "RequirementID": "REQ001",
                "Description": "Total mass requirement",
                "Parameter": "calculated_total_mass_kg",
                "Operator": "<=",
                "Value": "350"
            }
        ]
        
        engine = PropagationEngine(PropagationStrategy.AUTOMATIC)
        updated_bom, _, satisfied = engine.propagate_changes(
            changes,
            bom_data,
            requirements_data
        )
        
        t001_component = next(c for c in updated_bom['components'] if c['type'] == 'T001')
        self.assertEqual(t001_component['mass_kg_per_unit'], 95.0)
        self.assertEqual(updated_bom['calculated_total_mass_kg'], 357.0)
        self.assertFalse(satisfied)
    
    def test_mass_calculation(self):
        """Test total mass calculation."""
        bom_data = {
            "components": [
                {"type": "T001", "quantity": 2, "mass_kg_per_unit": 85.0},
                {"type": "SP001", "quantity": 2, "mass_kg_per_unit": 55.0},
            ],
            "calculated_total_mass_kg": 280.0
        }
        
        total = sum(
            c.get('quantity', 1) * c.get('mass_kg_per_unit', 0)
            for c in bom_data['components']
        )
        
        self.assertEqual(total, 280.0)


class TestConstraintEvaluation(unittest.TestCase):
    """Tests for constraint evaluation."""
    
    def test_constraint_operators(self):
        """Test various constraint operators."""
        engine = PropagationEngine()
        
        self.assertTrue(engine._evaluate_constraint(100.0, "<=", 150.0))
        self.assertFalse(engine._evaluate_constraint(200.0, "<=", 150.0))
        self.assertTrue(engine._evaluate_constraint(100.0, ">=", 50.0))
        self.assertFalse(engine._evaluate_constraint(100.0, ">=", 150.0))


if __name__ == "__main__":
    unittest.main()
