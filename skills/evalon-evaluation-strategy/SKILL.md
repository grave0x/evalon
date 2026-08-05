---
name: evalon-evaluation-strategy
description: Inspect an agent and produce a source-grounded agent profile, risk model, strategy-only evaluation design, and review. Use when deciding what an agent does, how it can fail, and how it should be evaluated. Do not generate evaluation cases, coverage datasets, adapters, suite or runner configuration, or execution-readiness assessments.
---

# Design an agent evaluation strategy

Answer one question: what does this agent do, what can go wrong, and how should that risk be measured?

Inspect the implementation and observable behavior. Preserve important risks even when Evalon cannot currently measure them. Stop after completing and delivering the strategy. Do not edit the target agent, create cases, configure or run evaluations, or assess execution readiness.

## Output

Write exactly these artifacts under `<target-project-root>/evalon-evals/<agent-slug>/`:

```text
agent-profile.yaml
risk-model.yaml
eval-strategy.yaml
review.yaml
```

Copy the four templates and replace all placeholders with source-grounded content:

```sh
skill_dir="<path-to-this-skill>"
draft_dir="<target-project-root>/evalon-evals/<agent-slug>"
mkdir -p "$draft_dir"
cp "$skill_dir/templates/agent-profile.yaml" "$skill_dir/templates/risk-model.yaml" \
  "$skill_dir/templates/eval-strategy.yaml" "$skill_dir/templates/review.yaml" "$draft_dir/"
```

Use the workflow phases in `agent-profile.yaml` in order: `profiling`, `risk_modeled`, `strategy_designed`, `reviewed`. Set the final phase to `reviewed` only after the review is complete. There is no approval checkpoint.

Never create `coverage.yaml`, `cases.yaml`, `suite.yaml`, adapters, fixtures, runner configuration, execution results, readiness reports, credentials, setup commands, or candidate invocation commands. Those belong to later workflows.

## Coordinate read-only scouts

For a non-trivial repository, prefer read-only scouts when available. First map the repository, then assign each relevant file or path pattern to one scout. Give each scout a bounded question, owned and excluded paths, and require paths plus lines or symbols, confidence, enforcement, unknowns, and intentionally uninspected paths. Scouts must not edit, delegate, or synthesize the final design.

Record assignments in `inspection_scope.scout_assignments`. Reconcile scout reports against the sources before writing conclusions. Production code is strongest evidence; tests and recorded observations show exercised behavior, not complete behavior. Documentation and prompts state intent unless code or infrastructure enforces them.

## Phase 1: profile

Follow the request path through input and output contracts, instructions, orchestration, model calls, tools, side effects, state, external services, knowledge sources, policies, observability, tests, limitations, and unknowns. Capture important facts once in the `claims` ledger, with source references, `confidence` of `verified|inferred|unknown`, and `enforcement` of `code|infrastructure|instruction|none`. Other profile sections reference claim IDs rather than repeating unsourced conclusions.

Record checkout provenance: an existing absolute local root, VCS kind, revision, branch or detached state, dirty state, and inspection time. Every `sources` ledger entry names exactly one file relative to that root, with current valid positive `N` or `N-M` line ranges. Never combine locations or use absolute paths or parent traversal. For non-Git material, record graceful `unknown` values rather than inventing Git facts. Record source conflicts instead of silently resolving them.

Do not label prompts as code enforcement. Do not infer observable fields without inspecting their contracts or implementation. Recorded candidate output and observability traces are evidence only; they are not independent truth unless independently verified.

## Phase 2: model risk

For each important capability, record a capability-to-risk disposition. Model plausible failures with intended behavior, consequence, severity, likelihood, observable and missing evidence, source and claim support, confidence, controls, and abstract `stimulus_requirements`.

Stimulus requirements define required conditions, prohibited effects, safety, isolation, reset, and fidelity. They are not evaluation records. Use severity `critical|high|medium|low` and likelihood `high|medium|low|unknown`. Keep severe risks visible when evidence is absent or current Evalon cannot express a measurement.

Consider incorrect output, tool choice, arguments or ordering, excessive or destructive actions, missing inspection, partial failure, retries, timeouts, malformed output, policy violations, injection, secret exposure, state leakage, service drift, cost, latency, and capability interactions.

## Phase 3: design strategy

Map every risk to a strategy disposition. A strategy separates the evaluated property from its execution shape, evidence, evaluator, independent truth, measurement status, pass semantics, and proof scope:

- Define exactly one precise `target_property`. It has a name, definition, and one or more atomic `criteria`. Each criterion has only an ID and an evaluator-independent condition about actual agent behavior or evidence. Never phrase a criterion in terms of a judge, evaluator, score, verdict, classification, rating, approval, positive result, or returned pass result.
- State `execution_shape.interaction_kind`, dependency-behavior `environment_mode`, reset requirements, and fidelity requirements. `environment_mode` is exactly `live`, `frozen`, `simulated`, `hybrid`, `not_applicable`, or `unknown`; keep isolation only in isolation requirements.
- Select enum-backed `evidence_sources`; evidence is not automatically truth. It must be non-empty for fully or partially measurable strategies, and may be empty when measurement is unavailable or unknown. A recorded target output or observability trace is still not independent truth.
- Set `evaluator.method` to `deterministic_assertion`, `python_check`, `model_judge`, `human_review`, or `none`, and independently state the current `evaluator.evalon_type` as `static`, `python`, `llm_judge`, `none`, or `unknown`. Fully and partially measurable strategies must use a non-`none` method: deterministic assertions map to `static`, Python checks to `python`, model judges to `llm_judge`, and human review to `none`. Unavailable strategies use `none` for both fields. Unknown strategies use `method: none` with `evalon_type: none` or `unknown`, and must preserve an honest residual gap.
- State whether independent truth is available, its source or description, and limitations. Never claim an oracle that does not exist.
- Set `measurement_status` to `fully_measurable`, `partially_measurable`, `not_currently_measurable`, or `unknown`.
- For every measurement status except `not_currently_measurable`, define `pass_if_and_only_if` exactly as `{scope: target_property, rule: all_criteria_hold, criterion_refs: [...]}`. Its references must be the complete, exact set of target criterion IDs. It contains no free-form evaluator-output pass sentence. For `not_currently_measurable`, it is null. State `proof_scope.proves`, `does_not_prove`, and residual gaps.

For `fully_measurable`, independent truth availability is `available` and proof residual gaps are empty. Partial, unavailable, and unknown measurement must name honest residual gaps. For `not_currently_measurable`, both evaluator values are `none` and `pass_if_and_only_if` is null. Do not use a weak proxy as if it proved a broader property.

Map coverage in both directions. Every strategy has a non-empty `risk_refs` set. Each capability disposition's `risk_refs` is exactly the set of risks whose `capability_refs` name it: a non-empty set is `modeled`, and `no_material_risk` has an empty set. Each risk disposition's `strategy_refs` is exactly the set of strategies whose `risk_refs` name it. Derive each risk disposition from all referenced strategy measurement statuses: all fully measurable means `fully_measured`; a set limited to fully and partially measurable with one partial means `partially_measured`; all unavailable means `not_currently_measurable`; all unknown means `unknown`; any other mixture means `unknown`. A risk without strategy references may only be unavailable or unknown and needs a residual gap. A fully measured risk has a null residual gap. Every other disposition has an honest non-empty residual gap.

Ground every claim and risk with non-empty `source_refs`, and every capability with non-empty `claim_refs`. Use only schema fields defined by the templates in controlled records. Do not include implementation or readiness fields such as cases, suites, runners, adapters, fixtures, setup commands, and readiness reports anywhere in the four documents.

The top-level conceptual architecture in `eval-strategy.yaml` has three boundaries only:

- Harness captures candidate invocation evidence only.
- Environment covers dependency mode, isolation, fidelity, and reset requirements only.
- Evaluation compares with independent truth and judges the target property only.

Each boundary prohibits callables, adapters, fixtures, credentials, setup commands, and execution-readiness claims or configuration. Do not turn expressibility research into an execution plan.

Expressibility is an evaluator-contract audit only: whether the selected current Evalon evaluator can judge the named target property if its declared evidence is supplied. Every entry must literally set `evidence_assumption: declared_evidence_is_supplied`; it remains `fully|partially|not_currently|unknown` and must use `basis: evaluator_contract_only`. Locate the current evaluator contract from an accessible Evalon checkout, installed package, or authoritative contract source, then record it in the top-level `evalon_contract_sources` ledger with its ID, kind, absolute local `root`, one root-relative file `location`, current valid line ranges, symbol, revision or version, lowercase `content_sha256`, non-empty `establishes` capability slugs, and notes. Each exact range must establish the particular evaluator predicate, context payload field, judge input, or validation behavior claimed by the strategy. Registry, routing, and evaluator-type enum lines alone are insufficient. Every known `current_evalon_expressibility` block names non-empty `required_contract_capabilities`; each must be present in `establishes` for at least one referenced contract source. The hash must equal the current bytes of that resolved file, so a dirty or untracked contract is pinned even if its revision names clean HEAD. `current_evalon_expressibility.source_refs` may reference only this ledger, never the target agent profile source ledger.

For `fully`, record non-empty supported parts, no missing parts, a null unknown reason, and at least one evaluator-contract source reference. For `partially`, record non-empty supported and missing parts, a null unknown reason, and at least one evaluator-contract source reference. For `not_currently`, record non-empty missing parts, a null unknown reason, and at least one evaluator-contract source reference; supported parts may be empty. Use `unknown` only when the evaluator contract cannot be inspected; give a non-empty reason, and source refs may be empty. Do not investigate adapters, runners, candidate callables, credentials, fixtures, environment setup, or execution readiness to decide expressibility.

Under that supplied-evidence assumption, `supported_parts` and `missing_parts` may describe only evaluator API fields, predicates, schemas, or judge capabilities. Target evidence and instrumentation gaps belong in `measurement_status`, required evidence, proof residual gaps, risks, and review observability gaps. They must not lower Evalon expressibility. Expressibility does not establish that target evidence exists, is trustworthy, can be collected, or is execution-ready.

Use exactly these failure attributions: `missing_evidence`, `infrastructure`, `evaluator`, `dependency`, `ambiguous`, and `agent`. Apply the declared precedence. Every non-agent attribution is `indeterminate`; only isolated agent evidence may yield `fail`. Keep candidate execution errors, evaluator errors, and target-property failures distinct.

## Phase 4: review and deliver

Review source coverage, conflicts, unknowns, severe risks, capability and risk dispositions, weak proxies, missing evidence, false-pass and false-fail conditions, safety, interaction gaps, and Evalon expressibility gaps. The severity ledger must list exactly every critical and high risk ID, with no omissions or extras. Explicitly review target criteria against complete pass references; measurement status against risk disposition; reverse capability/risk and strategy/risk mappings; claim, risk, and capability grounding; and whether every exact Evalon contract range establishes each strategy's required contract capability, as well as current contract hashes. Set `agent-profile.yaml` to `reviewed` and `review.yaml` status to `complete` only when this is done.

Deliver the four paths and the findings, then stop. Do not ask for approval or continue into cases, implementation, configuration, or evaluation execution.
