#!/usr/bin/env python3
"""
MT.1 -- Change Impact & Reconciliation

This module implements change detection, impact analysis, and propagation
for the satellite configuration model management challenge.

The task addresses ES.1 where a manufacturer updates the thruster mass
in the Parts Catalogue, which must be propagated to the Bill of Materials and Report.
"""

import json
import xml.etree.ElementTree as ET
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from enum import Enum
import hashlib


class PropagationStrategy(Enum):
    """Propagation strategies for change management."""
    MANUAL = "manual"
    SEMI_AUTOMATIC = "semi-automatic"
    AUTOMATIC = "automatic"


class GranularityLevel(Enum):
    """Granularity levels for change propagation."""
    ELEMENT = "element"
    FEATURE = "feature"
    MODEL = "model"


@dataclass
class Change:
    """Represents a detected change in a model."""
    source_file: str
    component_id: str
    attribute: str
    old_value: Any
    new_value: Any
    change_hash: str = ""
    
    def __post_init__(self):
        """Generate change hash for tracking."""
        change_str = f"{self.component_id}:{self.attribute}:{self.old_value}:{self.new_value}"
        self.change_hash = hashlib.md5(change_str.encode()).hexdigest()[:8]


@dataclass
class ImpactedElement:
    """Represents an element impacted by a change."""
    model_file: str
    element_id: str
    attribute: str
    old_value: Any
    new_value: Any
    granularity: GranularityLevel


class ChangeDetector:
    """Detects changes between model versions."""
    
    @staticmethod
    def detect_owl_changes(catalogue_v1: str, catalogue_v2: str) -> List[Change]:
        """
        Detect changes in OWL ontology files.
        
        Args:
            catalogue_v1: Path to baseline catalogue
            catalogue_v2: Path to updated catalogue
            
        Returns:
            List of detected changes
        """
        changes = []
        
        try:
            tree_v1 = ET.parse(catalogue_v1)
            tree_v2 = ET.parse(catalogue_v2)
            
            root_v1 = tree_v1.getroot()
            root_v2 = tree_v2.getroot()
            
            # Register namespaces
            namespaces = {
                'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
                'owl': 'http://www.w3.org/2002/07/owl#',
                'vim4': 'http://bipm.org/jcgm/vim4#',
            }
            
            for prefix, uri in namespaces.items():
                ET.register_namespace(prefix, uri)
            
            # Find all quantity values (mass values in this case)
            quantity_values_v1 = ChangeDetector._extract_quantity_values(root_v1, namespaces)
            quantity_values_v2 = ChangeDetector._extract_quantity_values(root_v2, namespaces)
            
            # Compare quantities
            for comp_id, (attr, value_v1) in quantity_values_v1.items():
                if comp_id in quantity_values_v2:
                    attr_v2, value_v2 = quantity_values_v2[comp_id]
                    if value_v1 != value_v2:
                        change = Change(
                            source_file="catalogue.owl",
                            component_id=comp_id,
                            attribute=attr,
                            old_value=value_v1,
                            new_value=value_v2
                        )
                        changes.append(change)
        
        except ET.ParseError as e:
            print(f"Error parsing OWL file: {e}")
        
        return changes
    
    @staticmethod
    def _extract_quantity_values(root: ET.Element, namespaces: Dict) -> Dict[str, Tuple[str, float]]:
        """Extract quantity values from OWL root element."""
        quantities = {}
        
        # Find all named individuals with mass values
        for individual in root.findall('.//owl:NamedIndividual', namespaces):
            about = individual.get('{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about', '')
            
            # Extract component ID
            comp_id = None
            comp_id_elem = individual.find('componentID')
            if comp_id_elem is not None:
                comp_id = comp_id_elem.text
            
            # Look for mass values
            for value_elem in individual.findall('vim4:hasDoubleNumber', namespaces):
                if comp_id:
                    value = float(value_elem.text)
                    quantities[comp_id] = ("mass", value)
        
        return quantities
    
    @staticmethod
    def detect_json_changes(bom_v1: str, bom_v2: str) -> List[Change]:
        """
        Detect changes in JSON Bill of Materials.
        
        Args:
            bom_v1: Path to baseline BOM
            bom_v2: Path to updated BOM
            
        Returns:
            List of detected changes
        """
        changes = []
        
        try:
            with open(bom_v1, 'r') as f:
                data_v1 = json.load(f)
            with open(bom_v2, 'r') as f:
                data_v2 = json.load(f)
            
            # Create component lookup
            comp_v1 = {c['type']: c for c in data_v1.get('components', [])}
            comp_v2 = {c['type']: c for c in data_v2.get('components', [])}
            
            # Compare component masses
            for comp_id, comp_v1_data in comp_v1.items():
                if comp_id in comp_v2:
                    comp_v2_data = comp_v2[comp_id]
                    
                    for key in ['mass_kg_per_unit', 'quantity']:
                        if comp_v1_data.get(key) != comp_v2_data.get(key):
                            change = Change(
                                source_file="bom.json",
                                component_id=comp_id,
                                attribute=key,
                                old_value=comp_v1_data.get(key),
                                new_value=comp_v2_data.get(key)
                            )
                            changes.append(change)
        
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error reading JSON file: {e}")
        
        return changes


class ImpactAnalyzer:
    """Analyzes impact of detected changes across models."""
    
    def __init__(self, bom_file: str, requirements_file: str, report_file: str):
        """Initialize impact analyzer with model references."""
        self.bom_file = bom_file
        self.requirements_file = requirements_file
        self.report_file = report_file
        self.impacted_elements: List[ImpactedElement] = []
    
    def analyze_impact(self, changes: List[Change], granularity: GranularityLevel = GranularityLevel.ELEMENT) -> List[ImpactedElement]:
        """
        Analyze impact of changes across dependent models.
        
        Args:
            changes: List of detected changes
            granularity: Level of granularity for impact analysis
            
        Returns:
            List of impacted elements
        """
        self.impacted_elements = []
        
        for change in changes:
            if change.source_file == "catalogue.owl" and change.component_id == "T001":
                # T001 mass change propagates to BOM
                self.impacted_elements.append(ImpactedElement(
                    model_file="bom.json",
                    element_id=change.component_id,
                    attribute="mass_kg_per_unit",
                    old_value=change.old_value,
                    new_value=change.new_value,
                    granularity=granularity
                ))
                
                # Impacts calculated total mass
                self.impacted_elements.append(ImpactedElement(
                    model_file="bom.json",
                    element_id="total_system",
                    attribute="calculated_total_mass_kg",
                    old_value=None,  # Will be computed
                    new_value=None,  # Will be computed
                    granularity=GranularityLevel.MODEL
                ))
                
                # Impacts report (requirement verification)
                self.impacted_elements.append(ImpactedElement(
                    model_file="report.md",
                    element_id="REQ001",
                    attribute="status",
                    old_value=None,
                    new_value=None,
                    granularity=GranularityLevel.MODEL
                ))
        
        return self.impacted_elements
    
    def build_impact_structure(self) -> Dict[str, List[str]]:
        """
        Build an explicit structure (graph/tree) representing impacted elements.
        
        Returns:
            Dictionary representing dependency graph
        """
        impact_graph = {
            "source": {"model": "catalogue.owl", "element": "T001", "attribute": "mass"},
            "propagation_chain": [
                {
                    "step": 1,
                    "from": "catalogue.owl:T001:mass",
                    "to": "bom.json:T001:mass_kg_per_unit",
                    "type": "direct_update Og lalalala"
                },
                {
                    "step": 2,
                    "from": "bom.json:T001:mass_kg_per_unit",
                    "to": "bom.json:total_system:calculated_total_mass_kg",
                    "type": "derived_computatio nøa nøanøn n"
                },
                {
                    "step": 3,
                    "from": "bom.json:total_system:calculated_total_mass_kg",
                    "to": "report.md:REQ001:status",
                    "type": "requirement_verification koo kokoko kokokoko"
                }
            ],
            "impacted_elements": [elem.__dict__ for elem in self.impacted_elements]
        }
        
        return impact_graph


class PropagationEngine:
    """Executes change propagation according to specified strategy."""
    
    def __init__(self, strategy: PropagationStrategy = PropagationStrategy.SEMI_AUTOMATIC):
        """Initialize with propagation strategy."""
        self.strategy = strategy
        self.propagation_log = []
    
    def propagate_changes(self, changes: List[Change], bom_data: Dict, requirements_data: List[Dict]) -> Tuple[Dict, List[Dict], bool]:
        """
        Propagate changes to dependent models.
        
        Args:
            changes: List of detected changes
            bom_data: Current BOM data
            requirements_data: Current requirements data
            
        Returns:
            Tuple of (updated_bom, updated_requirements, all_satisfied)
        """
        updated_bom = bom_data.copy()
        updated_bom['components'] = [c.copy() for c in bom_data.get('components', [])]
        
        for change in changes:
            if change.component_id == "T001" and change.attribute == "mass":
                # Update BOM component
                for component in updated_bom['components']:
                    if component.get('type') == 'T001':
                        self.propagation_log.append({
                            'action': 'update',
                            'model': 'bom.json',
                            'component': 'T001',
                            'old_value': component.get('mass_kg_per_unit'),
                            'new_value': change.new_value
                        })
                        component['mass_kg_per_unit'] = change.new_value
        
        # Recalculate total mass
        total_mass = sum(
            c.get('quantity', 1) * c.get('mass_kg_per_unit', 0)
            for c in updated_bom['components']
        )
        
        self.propagation_log.append({
            'action': 'recalculate',
            'model': 'bom.json',
            'attribute': 'calculated_total_mass_kg',
            'old_value': bom_data.get('calculated_total_mass_kg'),
            'new_value': total_mass
        })
        
        updated_bom['calculated_total_mass_kg'] = total_mass
        
        # Verify requirements
        all_satisfied = True
        for req in requirements_data:
            param = req.get('Parameter', '')
            operator = req.get('Operator', '')
            threshold = float(req.get('Value', 0))
            
            if param == 'calculated_total_mass_kg':
                actual = updated_bom['calculated_total_mass_kg']
                satisfied = self._evaluate_constraint(actual, operator, threshold)
                
                self.propagation_log.append({
                    'action': 'verify',
                    'requirement': req.get('RequirementID'),
                    'satisfied': satisfied,
                    'actual_value': actual,
                    'threshold': threshold
                })
                
                all_satisfied = all_satisfied and satisfied
        
        return updated_bom, requirements_data, all_satisfied
    
    @staticmethod
    def _evaluate_constraint(actual: float, operator: str, threshold: float) -> bool:
        """Evaluate a constraint against actual value."""
        if operator == "<=":
            return actual <= threshold
        elif operator == ">=":
            return actual >= threshold
        elif operator == "<":
            return actual < threshold
        elif operator == ">":
            return actual > threshold
        elif operator == "==":
            return actual == threshold
        else:
            return False


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python mt1_change_impact.py <v1_catalogue> <v2_catalogue>")
        sys.exit(1)
    
    catalogue_v1 = sys.argv[1]
    catalogue_v2 = sys.argv[2]
    
    # Detect changes
    print("="*70)
    print("MT.1 -- Change Impact & Reconciliation")
    print("="*70)
    
    detector = ChangeDetector()
    changes = detector.detect_owl_changes(catalogue_v1, catalogue_v2)
    
    print(f"\n1. CHANGE DETECTION")
    print(f"   Detected {len(changes)} change(s):")
    for change in changes:
        print(f"   - {change.component_id}.{change.attribute}: {change.old_value} → {change.new_value} (hash: {change.change_hash})")
    
    # Analyze impact
    print(f"\n2. IMPACT ANALYSIS")
    analyzer = ImpactAnalyzer("bom.json", "requirements.csv", "report.md")
    impacted = analyzer.analyze_impact(changes, GranularityLevel.ELEMENT)
    print(f"   Identified {len(impacted)} impacted element(s):")
    for elem in impacted:
        print(f"   - {elem.model_file}:{elem.element_id}:{elem.attribute}")
    
    # Build impact structure
    impact_graph = analyzer.build_impact_structure()
    print(f"\n3. IMPACT STRUCTURE (Dependency Graph)")
    print(f"   Source: {impact_graph['source']['model']}:{impact_graph['source']['element']}:{impact_graph['source']['attribute']}")
    print(f"   Propagation chain ({len(impact_graph['propagation_chain'])} steps):")
    for step in impact_graph['propagation_chain']:
        print(f"     Step {step['step']}: {step['from']} → {step['to']} ({step['type']})")
