---
name: evalon-audit
description: Inspect an agent and produce a source-grounded agent profile, risk model, and review. Use when deciding what an agent does and how it can fail. Do not generate evaluation cases, coverage datasets, adapters, suite or runner configuration, or execution-readiness assessments.
---

# Analyze agent evaluation risks

Answer two questions: what does this agent do, and what can go wrong?

Inspect the implementation and observable behavior. Preserve important risks even when evidence is incomplete. Stop after completing and delivering the analysis. Do not edit the target agent, create cases, configure or run evaluations, or assess execution readiness.

## Output

Write exactly these artifacts under `<target-project-root>/evalon-evals/<agent-slug>/`:

```text
agent-profile.yaml
risk-model.yaml
review.yaml
```

Copy the three templates and replace all placeholders with source-grounded content:

```sh
skill_dir="<path-to-this-skill>"
draft_dir="<target-project-root>/evalon-evals/<agent-slug>"
mkdir -p "$draft_dir"
cp "$skill_dir/templates/agent-profile.yaml" "$skill_dir/templates/risk-model.yaml" \
  "$skill_dir/templates/review.yaml" "$draft_dir/"
```

Never create `coverage.yaml`, `cases.yaml`, `suite.yaml`, adapters, fixtures, runner configuration, execution results, readiness reports, credentials, setup commands, or candidate invocation commands. Those belong to later workflows.

## Coordinate read-only scouts

For a non-trivial repository, prefer read-only scouts when available. First map the repository, then assign each relevant file or path pattern to one scout. Give each scout a bounded question, owned and excluded paths, and require paths plus lines or symbols, confidence, enforcement, unknowns, and intentionally uninspected paths. Scouts must not edit, delegate, or synthesize the final design.

Reconcile scout reports against the sources before writing conclusions. Production code is strongest evidence; tests and recorded observations show exercised behavior, not complete behavior. Documentation and prompts state intent unless code or infrastructure enforces them.

## Phase 1: profile

Follow the request path through input and output contracts, instructions, orchestration, model calls, tools, side effects, state, external services, observability, tests, limitations, and unknowns. Capture important facts once in the `claims` ledger, with source references, `confidence` of `verified|inferred|unknown`, and `enforcement` of `code|infrastructure|instruction|none`. Other profile sections reference claim IDs rather than repeating unsourced conclusions.

Record checkout provenance: an existing absolute local root, revision, dirty state, and inspection time. Every `sources` ledger entry names exactly one file relative to that root, with current valid positive `N` or `N-M` line ranges. Never combine locations or use absolute paths or parent traversal. For non-Git material, record graceful `unknown` values rather than inventing Git facts. Record source conflicts as review findings instead of silently resolving them.

Do not label prompts as code enforcement. Do not infer observable fields without inspecting their contracts or implementation. Recorded candidate output and observability traces are evidence only; they are not independent truth unless independently verified.

## Phase 2: model risk

For each important capability, model plausible failures with consequence, severity, evidence, observability gaps, source support, confidence, and existing controls. A risk involving multiple capabilities references each one in `capability_refs`.

Stimulus requirements define required conditions, prohibited effects, safety, isolation, reset, and fidelity. They are not evaluation records. Use severity `critical|high|medium|low` and likelihood `high|medium|low|unknown`. Keep severe risks visible when evidence is absent or current Evalon cannot express a measurement.

Consider incorrect output, tool choice, arguments or ordering, excessive or destructive actions, missing inspection, partial failure, retries, timeouts, malformed output, policy violations, injection, secret exposure, state leakage, service drift, cost, latency, and capability interactions.

## Phase 3: review and deliver

Review source coverage, conflicts, unknowns, severe risks, unsupported risk assumptions, safety, capabilities without risks, and interaction gaps. The severity ledger must list exactly every critical and high risk ID, with no omissions or extras. Confirm that every claim, risk, and capability has the required source support. Set `review.yaml` status to `complete` only when this is done.

Deliver the three paths and the findings, then stop. Do not ask for approval or continue into cases, implementation, configuration, or evaluation execution.
