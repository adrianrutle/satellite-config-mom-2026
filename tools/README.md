# MT.1 -- Change Impact & Reconciliation Implementation

This directory contains the implementation of **MT.1 -- Change Impact & Reconciliation** from the Model Management Challenge. It uses only the Python standard library (Python 3.10+).

## Overview

Each version directory (`v1/`, `v2/`, ...) is a *snapshot* of the five artefacts. The tools never change the formats or the source artefacts owned by other roles. They only maintain the artefacts derived from them:

| Source artefact | Derived element | Rule |
|---|---|---|
| `catalogue.owl` (mass of `<id>` via the VIM4 value chain) | `bom.json` `mass_kg_per_unit` of `<id>` | copy value |
| `comm-network.json` (nodes per type) | `bom.json` `quantity` of each comms component | count nodes; add or remove entries |
| `bom.json` components | `bom.json` `calculated_total_mass_kg` | Σ quantity × mass |
| `bom.json` | `bom.xml` | same content in XML |
| `comm-network.json` | `comm-network.xml` | same content in XML |
| `requirements.csv` + `bom.json` | `report.md` | evaluate each requirement and render the table |

## Files

- **`artefacts.py`**: loaders for the five artefacts and renderers for the derived representations. Rendering `v1/` reproduces the committed files byte for byte.
- **`mt1_change_impact.py`**: the core module.
  - `ChangeDetector`: diffs two snapshots (catalogue masses and classes, BOM entries, network nodes, links and renames, requirements).
  - `ImpactAnalyzer`: traces each change through the rules above into an `ImpactGraph`. Graphs from several changes compose into one graph, can be inspected path by path, and can be coarsened to element, feature or model granularity.
  - `PropagationEngine`: reconciles a snapshot according to a `PropagationStrategy`.
- **`mt1_orchestrate.py`**: command line interface (`diff`, `check`, `propagate`, `run`).
- **`test_mt1.py`**: unit and end-to-end tests. Each end-to-end test edits one source artefact in a copy of a snapshot and asserts that the change reaches every dependent artefact.

## Usage

Run from the repository root:

```bash
python3 tools/mt1_orchestrate.py diff v1 v2                       # change detection + impact graph
python3 tools/mt1_orchestrate.py diff v1 v2 --granularity model   # impact per artefact
python3 tools/mt1_orchestrate.py check v1 v2                      # exit 1 if anything is out of sync
python3 tools/mt1_orchestrate.py propagate v2 --strategy automatic
python3 tools/mt1_orchestrate.py --json mt1_results.json run v1 v2
```

Strategies:

| Strategy | Behaviour |
|---|---|
| `manual` | Computes the full plan and changes no files (used by `check`). |
| `semi-automatic` | Applies value updates (masses, total, XML mirrors, report). Structural BOM edits (adding, removing or merging entries, quantity changes from the communications view) are listed as *pending* for review. |
| `automatic` | Applies every update. |

`--baseline <dir>` gives the previous snapshot to `propagate`. With it, a component type that disappears from the communications view entirely can also be removed from the BOM.

## CI/CD

- **`.github/workflows/ci.yml`** runs on every push and pull request:
  1. Runs the unit tests on Python 3.10 and 3.12.
  2. On a pull request, writes the change impact of each snapshot against the base branch to the job summary.
  3. Runs `check v*/`. When it fails, it adds the patch that `propagate --strategy automatic` would produce to the summary and uploads it as an artifact.
- **`.github/workflows/propagate.yml`** runs on pushes to `main` that touch `v*/` or `tools/`, and can also be started manually:
  1. Diffs each snapshot against the previous commit.
  2. Propagates the changes with that commit as baseline.
  3. Uploads the JSON results.
  4. Opens (or updates) a pull request `mom/propagate-main` with the regenerated artefacts. The run summary becomes the pull request description.

  For this, the repository setting *Settings → Actions → General → Allow GitHub Actions to create and approve pull requests* must be enabled.

## MT.1 Question Answers

| # | Question | Answer |
|---|----------|--------|
| 1 | How is change detected? | Snapshot diff per element (`ChangeDetector`). In CI the diff is triggered by a git push or pull request against the previous commit. |
| 2 | Propagation mechanisms? | Explicit traceability rules between artefacts (table above), applied by `PropagationEngine.reconcile`. |
| 3 | Automatic/semi/manual? | All three; see *Strategies*. |
| 4 | Parameterised propagation? | Yes: `--strategy`, plus `workflow_dispatch` input in CD. |
| 5 | Propagation granularity? | Element (`bom.json:T001:mass_kg_per_unit`), feature (`bom.json:mass_kg_per_unit`) or model (`bom.json`); `--granularity`. |
| 6 | Explicit structure for impacts? | `ImpactGraph` (nodes, typed edges, roots per change hash), exported with `--json`. |
| 7 | Composable across changes? | Yes. Graphs are merged, and each node records which changes reach it. `paths_from` lists every propagation path from a change. |

## Known limitations

- `report.docx` and the `.ods`/`.xlsx` requirement files are not read or regenerated. `requirements.csv` is the reference.
- Communications components are identified as the types that appear in `comm-network.json` (or in the baseline snapshot's network).
