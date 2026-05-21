# MT.1 -- Change Impact & Reconciliation Implementation

This directory contains the implementation of **MT.1 -- Change Impact & Reconciliation** from the Model Management Challenge.

## Overview

MT.1 addresses **Engineering Scenario ES.1**, where a parts manufacturer updates component specifications in the Parts Catalogue. The implementation demonstrates:

1. **Change Detection**: Identifies modifications in source models (OWL ontology)
2. **Impact Analysis**: Traces affected elements across dependent models
3. **Dependency Mapping**: Builds explicit structures representing change propagation
4. **Change Propagation**: Applies updates with configurable automation levels
5. **Verification**: Validates requirements after propagation

## Files

### Core Implementation

- **`mt1_change_impact.py`**: Core module implementing change detection, impact analysis, and propagation
  - `ChangeDetector`: Detects changes in OWL and JSON models
  - `ImpactAnalyzer`: Analyzes impact and builds dependency graphs
  - `PropagationEngine`: Executes change propagation with configurable strategies

- **`mt1_orchestrate.py`**: Orchestration script that runs the complete workflow
  - `MT1Orchestrator`: Coordinates all MT.1 steps
  - Generates summary reports

### Testing & Documentation

- **`test_mt1.py`**: Unit tests covering all core functionality
- **`README.md`**: This file
- **`MT1_IMPLEMENTATION_GUIDE.md`**: Detailed architecture and extension guide

## Quick Start

```bash
cd tools/
python3 mt1_orchestrate.py .. --strategy semi-automatic
```

## MT.1 Question Answers

| # | Question | Answer |
|---|----------|--------|
| 1 | How is change detected? | XML/JSON parsing with component ID extraction |
| 2 | Propagation mechanisms? | Dependency tracing via component IDs with cascading updates |
| 3 | Automatic/semi/manual? | ✓ All supported via `PropagationStrategy` enum |
| 4 | Parameterised propagation? | ✓ Strategy configurable at runtime |
| 5 | Propagation granularity? | ✓ ELEMENT, FEATURE, or MODEL levels |
| 6 | Explicit structure for impacts? | ✓ Dependency graph with full chain |
| 7 | Composable across changes? | ✓ Sequential processing into unified graph |

## Testing

```bash
python3 -m unittest test_mt1.py -v
```

## Integration

This implementation serves as a foundation for MT.2-MT.5, providing:
- Change detection framework
- Dependency graph building
- Requirement verification
- Propagation logging for audit trails
