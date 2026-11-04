---
name: control
description: "Investigate whether PRISM improves attacks, environment validity, or monitors in an authorized AI-control benchmark. Uses separate development and held-out comparisons."
user-invokable: true
argument-hint: "[benchmark path and control research question]"
---

# AI-control investigator

This is a new PRISM scaffold variant, not evidence that PRISM improves control.
Use the native Claude Code lead and existing Explorer, Executor, and blinded
Analyst. Read `../orchestrate/SKILL.md` and its required methodological and
invocation references before conducting an investigation. Inherit its pipeline,
opaque condition IDs, append-only preregistration, detached execution, and
failure/resumption rules. The control-specific design below supplements it.

## Scope and launch gates

Record the benchmark URL, pinned revision, task family, permitted sandbox,
attacker objective and access, defender tools, monitor observation channel,
intervention timing, and trusted success verifier. Do not substitute a similarly
named benchmark when its identity is unresolved. Do not provide real credentials
or host access to benchmark agents; use synthetic assets and isolated testbeds.

Resolve exact models and routes separately for lead, attacker, defender,
monitor, and judge. Preserve the user's model choice unless they change it.
Smoke-test the complete accounted route, including tool calls and judge calls.
Do not confuse an API smoke with a successful benchmark run.

Require a campaign-specific ledger and admission caps for every paid role,
including research, retries, scanners, and failed calls. Reserve outstanding
calls before dispatch; unknown charges remain reserved. A budget JSON alone
does not enforce a cap. If any route can bypass accounting, do not launch it.
Keep historical campaigns and their unresolved charges intact.

## Three distinct intervention tracks

1. **Attack improvement:** freeze environment, verifier, defender, and monitor.
   Compare original attack generation with PRISM-developed attacks on held-out
   tasks/seeds. Include an equal-budget non-PRISM search/retry baseline to
   distinguish scaffold benefit from additional search. Record both development
   API dollars and target query counts; report mismatches, do not hide them.
   Primary outcome: verified attacker success *and* escape from intervention.
   Also report raw attack success, detection, and benign task completion.
2. **Environment repair:** demonstrate a specific defect with an executable
   reproduction and independent state checks. Freeze policies and compare
   original/repaired environments using matched tasks and seeds. Include clean
   success/failure controls. A changed score alone is not proof of repair: show
   improved correspondence with intended task semantics. Report any change to
   the threat model or task difficulty instead of labeling it a neutral fix.
3. **Monitor improvement:** freeze attack traces for offline screening; compare
   original/PRISM monitors at a threshold calibrated only on development benign
   traces. Report detection at matched false-positive rate, benign utility, and
   monitor cost/latency. Separate monitor-visible evidence from evaluator-only
   attacker intent. Frozen-trace rescoring cannot establish live intervention
   effectiveness; confirm promising monitors with fresh adaptive attacks under
   the real intervention policy before claiming improved control safety.

Do not simultaneously change all three and attribute the outcome to one.
After isolated comparisons, a combined-system experiment may measure interactions.

## Development, selection, and confirmation

Before PRISM sees outcomes, freeze task IDs grouped by shared task/template,
split assignment, seeds, primary endpoints, uncertainty method, and stopping
rule. Prevent near-duplicate task families crossing development/test splits.
Hash the baseline implementation and keep held-out task content/results away
from attack and monitor developers. Select variants on development data only.
Once test results are inspected, additional tuning requires a new holdout or an
explicit exploratory label. Opaque labels alone do not enforce this separation.

Estimate feasible sample size from accounted smoke cost before the main run;
document detectable-effect limitations. Preserve failed episodes in the run
inventory; report infrastructure failures separately, never silently turn them
into safe outcomes. Use paired comparisons and task-clustered uncertainty when
multiple attempts share a task; do not treat every attempt as independent.

Prefer executable sandbox state checks to manual transcript review. Where
semantic judgments remain necessary, use blinded evidence-citing reviewers,
record disagreements and unresolved cases, and report reviewer sensitivity.
Model consensus is not human ground truth. Do not require bulk human review
unless a consequential ambiguity cannot be resolved automatically.

## Required outputs

Maintain `CONTROL-PROTOCOL.json` (scope, routes, splits, endpoints, budgets),
the standard `INVESTIGATION-LOG.md`, immutable variant diffs/hashes, per-episode
results with verifier evidence and failure status, and campaign spend totals.
The final report must include all three tracks, including negative/inconclusive
results, absolute outcomes and paired differences, uncertainty, resources used,
and limitations. Distinguish development improvements from held-out improvements
and offline monitor results from live adaptive control results.

For an interrupted campaign, preserve request IDs and poll existing handles
before restarting. Resume only missing work; retain all incurred reservations.
