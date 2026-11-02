# Femm: event-driven federated model management

Femm keeps a federation of heterogeneous artefacts consistent without taking them
away from their owners. Its principle is *events report, commands propose*:

* every artefact is owned by one **Model Synchronization Agent (MSA)**, the only
  component that writes it;
* MSAs publish **events** when their artefact changes;
* the **Synchronization Planning** service turns events into **commands**, which
  are proposals addressed to the owner of one artefact;
* the owner, or the owner's execution policy, approves or rejects each command;
* an approved command is applied only if the artefact still has the version the
  command was computed against. Otherwise the MSA reports a conflict and the
  planner recomputes the step.

The bus only delivers messages. All knowledge is held by services, each of which
owns one explicit model: the federation specification (megamodel), impact graphs,
plans, validation results, and versions with baselines.

This package is the protocol core (Python standard library only) plus two
realisations: an in-memory one for tests and experiments, and a Git/GitHub one.
A Restate realisation is planned.

## Layout

| Module | Contents |
|---|---|
| `federation.py` | Federation specification F = (A, own, E, R): artefacts with owner and policy, rules (correspondence, derivation, representation), constraints. Also deltas, impact graphs, consistency, and the acyclicity check (A3). |
| `messages.py` | CloudEvents-style envelope with version, base version, `correlation_id` (plan) and `causation_id` |
| `msa.py` | MSA lifecycle (Consistent, Modified, Pending approval, Applying, Conflict), deduplication (A1), and a technology adapter (`Store`) |
| `services.py` | Impact Analysis, Synchronization Planning, Validation, Version |
| `bus.py` | In-memory bus. It can reorder and duplicate deliveries (fault injection) |
| `runtime.py` | `System`: wires one MSA per artefact and the services onto a bus |
| `satellite.py` | The MoM 2026 satellite federation. It uses the loaders and renderers in `tools/artefacts.py` as adapters |
| `gitflow.py` | Git realisation, described below |
| `tests/` | Protocol properties on random schedules, scenarios ES.1–ES.3, and the Git realisation on throw-away repositories |

## Try it

```bash
python3 -m unittest discover -s femm/tests -t .      # 29 tests
python3 -m femm scenario es1                         # message trace of ES.1
python3 -m femm scenario all --shuffle --seed 3 --duplicates 5
python3 -m femm scenario es2 --reject                # the owner rejects the backup antenna
python3 -m femm scenario es3                         # concurrent edits of the network
python3 -m femm check v2                             # consistency and requirement results
python3 -m femm demo-git                             # ES.1 + ES.2 on a throw-away Git repository
```

## Protocol properties, tested

`tests/test_protocol.py` runs the federation that is analysed in Maude
(catalogue → BoM → report, with a locally owned BoM part) on 300 random
schedules. Each schedule uses random delivery order, duplicated messages, local
edits at random points, and random approvals and rejections. The tests check:

* **Termination**: every run reaches quiescence.
* **Convergence**: the final state is consistent, unless a command was rejected.
* **No lost updates**: with the base-version check, no command overwrites a newer version. Without the check, a counterexample is found.
* **Authority**: only the owner writes its artefact, and only the planner sends commands.
* **Completion**: every plan completes, and a baseline is recorded after convergence.
* **The Maude regression**: the planner converges even with deduplication in the services switched off. A planner that advanced its view on conflicts fails this test, which mutation testing confirmed.

## Execution policies

Policies belong to the owner and are enforced by its MSA:

| Policy | Behaviour |
|---|---|
| `automatic` | every command is applied |
| `semi-automatic` | value updates are applied; structural changes (elements added or removed) wait for the owner |
| `manual` | every command waits for the owner |

A command that is already outdated when it arrives is reported as a conflict
straight away, without asking the owner.

## Git realisation (`gitflow.py`, `.github/workflows/femm.yml`)

| Protocol | Git / GitHub |
|---|---|
| artefact, owner | file under the federation root, with an owner in `femm.json` (`python3 -m femm git codeowners` prints CODEOWNERS lines) |
| version | blob id of the file on `main` |
| event | a push to `main` that changes the file |
| command | pull request `femm/<plan>/<command>` with a record in `.femm/log/<plan>/<command>.json` |
| approval / rejection | merging / closing the pull request. If the policy allows it, the planner merges at once |
| base-version check | the blob of the target on `main` must equal the record's `base_blob`. Otherwise the planner closes the pull request as a conflict and proposes the step again. The `femm/base-version` check does the same on the pull request itself |
| correlation (A4) | the command record merged with a change names its plan |
| one message at a time (A2) | a job concurrency group. The planner resumes from `refs/femm/processed`, so a dropped run loses no events |
| baseline | annotated tag `femm-baseline-<n>` when `main` is consistent and no command is open |
| event log, rationale | the commit history, the command records and the pull request discussions |

The workflow is off by default. To use it on GitHub:

1. Set the repository variable `FEMM_ENABLED=true` (Settings → Secrets and variables → Actions → Variables). This also switches off `propagate.yml`.
2. Allow GitHub Actions to create pull requests (Settings → Actions → General).
3. Optional: store a token with *contents* and *pull requests* write access as the secret `FEMM_TOKEN`. Pull requests created with the default `GITHUB_TOKEN` do not trigger other workflows, so without the token the `femm/base-version` check does not run on the planner's own pull requests. The planner itself still closes stale ones.
4. Optional: add `.github/CODEOWNERS` (from `python3 -m femm git codeowners`) and require code-owner review on `main`.

`femm.json` places the federation in `v2/`. The planner also runs locally against
any repository (`python3 -m femm git --forge local on-push --before A --after B`),
with pull requests simulated as local branches.
