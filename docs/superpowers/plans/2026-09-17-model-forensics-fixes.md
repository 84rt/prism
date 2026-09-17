# Model Forensics — Scaffold Fixes (divvy-able work-packages)

Derived from the six-run analysis in `2026-09-17-model-forensics-analysis.md` (a repo copy of `/workspaces/forensics-cases/analysis/ANALYSIS.md`), which records 38 deficiencies (`D01`–`D38`) across 8 themes with per-run evidence. This document turns those deficiencies into **implementable improvements grouped into 7 work-packages (WP1–WP7)**, each ownable by one Claude Code session with mostly disjoint file ownership so they can run in parallel. The per-session launch briefs are in the sibling `2026-09-17-model-forensics-fix-briefs.md`.

**How to use.** Each WP names the files it owns, the deficiencies it closes, a concrete fix per deficiency, and coordination notes where two WPs touch the same file. Priorities: **P0** blocks clean/valid operation, **P1** high-value (cost, firewall, measurement validity), **P2** hardening. Fixes are proposals — finalise the exact implementation per WP; the deficiency IDs let you trace each back to its evidence in the analysis. Where a fix has a mechanical half (a script, a runner change) and a policy half (a rule in a Diagnostician doc), the halves are assigned to different WPs and cross-referenced.

**Revision history.** 2026-09-17 first draft at `/workspaces/model-forensics-fixes.md` (container-local). Same day, revised and moved here after a repo read-through: WP1 re-designed around a deterministic core with the Executor agent kept as the judgement/recovery layer; the engine-level scorer leak named; WP2's proxy requirement stated; WP6 broadened from a staging step to opaque-at-source plus a leak check; WP4 given the two deterministic tools it actually needs; D02/D03/D28/D36-guard/D12-mechanism/D14-mechanism re-homed to WP1; D16's Diagnostician half moved to WP5-B; WP5 made sequential.

**Evidence lives outside the repo.** `/workspaces/forensics-cases/` (the six runs, ~730 MB without venvs: 175 `.eval` logs, persisted grader packages with hashes, label maps, request payloads, the in-house runner scripts) is container-local and is the regression corpus for WP3, WP4, WP6 and WP7's offline tests. Preserve it before a container rebuild.

---

## Design principle for this pass

**Deterministic core, agents for judgement.** Every failure the Executor sub-agent showed in six runs was a harness failure (foreground cap, backgrounding, permission denial, teardown), not a judgement failure, while the one judgement it exercised (concurrency) made things worse. The transient-failure handling the lead investigator must never see already lives in the script layer. So this pass pushes everything the contracts already make rule-bound *down into scripts that can be called with confidence and tested offline* — command construction, preflight, detached launch, score stripping, report assembly, ledger serialisation, report-conformance checks, blinding leak checks, transcript dumps — and keeps the sub-agents as the interface and the recovery layer, where judgement earns its cost. Both modes (`/orchestrate`, `/diagnose`) share the execution path; nothing here forks it.

---

## Priority summary

| WP | Title | Priority | Closes | Primary files (owner) |
|---|---|---|---|---|
| WP1 | Execution layer & launch infrastructure | **P0** | D01, D02, D03, D04, D24, D28, D36 (guard), D12 (mechanism), D14 (mechanism) | `scripts/execute_evals.py`, new detached launcher, `subagents/experiment_executor/*`, `executor_interface_contract.md`, `runner.py` (cost sidecar + turn cap only), `settings.json` allowlist |
| WP2 | Sub-agent routing & credentials | **P1** | D05, D27 | `runner.py` (route resolver), `subagent_invocation.md §routes`, every "no direct-OpenAI route" statement |
| WP3 | Analyst instruments & measurement validity | **P0** | D06, D07, D08, D14 (policy), D15, D16 (scanning-model half), D25, D30 | `analyst_forensic_addendum.md`, `subagents/transcript_analyst/*`, `diagnostician_analyst_guide.md`, named 3c/3d bullets of `diagnostician_responsibilities.md` |
| WP4 | Critic interface & candidate handoff | **P2** | D09, D10, D11, D33, D38 | `critic_interface_contract.md`, `subagents/redteam_critic/*`, new `scripts/ledger_to_json.py`, new `scripts/check_report.py` |
| WP5 | Diagnostician governance, budget & ledger economics | **P1** | D12 (policy), D13, D17, D18, D19, D20, D21, D29, D31, D34, D35, D37, D16 (Diagnostician half) | `diagnostician_responsibilities.md` (sole writer, A then B), `candidate-methodology.md`, `SKILL.md` autonomy section |
| WP6 | Blinding firewall | **P0** | D32 | new `scripts/check_blinding.py`, `designer_interface_contract.md`, the 3a/3c label-map wording, Analyst `RESTRICTED_FILES` |
| WP7 | References & logging hygiene | **P2** | D22, D23, D26, D36 (doc half) | `references/inspect_reference.md §Common gotchas`, `subagent_invocation.md` transcript-handoff note, new `scripts/dump_transcripts.py`, log template lint |

**Coordination hotspots.**
- `subagents/runner.py`: WP1 (cost sidecar, `subagent_max_turns`) vs WP2 (route resolver) — split by function; WP2 rebases after WP1 lands.
- `diagnostician_responsibilities.md`: WP5 is the sole writer of the doc *except* the paragraphs named below, which belong to other WPs and which WP5 leaves untouched: §Cross-family Critic gate error text (WP2); 3c "one invocation per check" bullet and 3d step 7 harvest wording (WP3); 3a apply-diffs/label-map and 3c opaque-labels bullets (WP6); 3b "Executor invocation" paragraph (WP1). Merges are hunk-disjoint; land WP5 first, then the others rebase.
- `subagent_invocation.md`: WP1 (§General Pattern launcher), WP2 (§`subagent_route`), WP3 (§Forensic mode), WP7 (transcript-handoff note) — disjoint sections.
- `subagents/transcript_analyst/agent.py`: WP3 (prompt builder for the combined-check field) vs WP6 (one `RESTRICTED_FILES` entry for the label map) — WP6 lands after WP3.
- `critic_interface_contract.md` and `SKILL.md`: WP2 owns only the sentence(s) claiming no direct-OpenAI route exists; WP4/WP5 own the rest respectively.
- Every WP that adds a file also edits the four mechanical surfaces (`install.sh` `link_file` list, `GITIGNORE_ENTRIES`, `uninstall.sh`, README file tree) and, for a script the Diagnostician or a sub-agent runs, `.claude/settings.json`. This is per-WP, not deferred to Wave 3; Wave 3 only re-checks.

---

## WP1 — Execution layer & launch infrastructure (P0)

*Every one of the six runs routed around the Experiment Executor sub-agent and executed evals via an ad-hoc score-blind driver (`run_condition.py` in R01/R04, `run_evals_blind*.sh` in R06). Both drivers wrap the scaffold's own `execute_evals.py` byte-for-byte unchanged; the missing component is the layer around it.* Owns the execution engine, the Executor sub-agent and its contract, and the launch mechanism every sub-agent uses.

**Design (settled 2026-09-17).** Keep the Executor agent as the interface the Diagnostician talks to and as the recovery layer for novel structural failures; make `scripts/execute_evals.py` a full batch runner so the agent has little left to get wrong.

- **D24 / D01 — foreground cap and background-and-bail.** The runner takes the *contract's* request JSON directly (experiment name, dir, conditions, models, routing, overrides), constructs the commands itself, runs preflight, launches the batch **detached** (`setsid`, own process group, survives the caller's teardown), and maintains a status file (per-command state, sample counts, wall-clock; never scores). It emits a **draft report in the contract's section shape** from the status file on request. The Executor agent's protocol becomes two short phases — *launch* (validate request, call the runner, confirm launch) and *report* (poll status at a sensible interval, read the draft, diagnose anything tagged structural, re-run once if a fix within the rules exists, emit the report). Its no-backgrounding hook stays: the detach lives in the script, not in the agent's Bash call.
- **Engine-level scorer leak (part of D01/D24; unnamed in the register).** `execute_evals.py` currently places scorer `metrics` from `read_eval_log` into the JSON the Executor reads (`verify_eval_log`), so the blackout rests on a prompt instruction over a leaking tool — both runs noticed and stripped it by hand. *Fix:* metrics never enter stdout or the status file; written, if at all, to a `.private.json` sidecar nothing in the pipeline reads. Unit-tested with a fixture log.
- **D02 — over-parallelisation.** A default concurrency ceiling in the runner sized for a shared host with one Docker sandbox per sample (`max_parallel: 1`, `max_connections: 4` as the starting defaults — R06's recovered values), overridable per request via `overrides`; the agent may raise it only with a recorded rationale in *Concurrency Decision*.
- **D03 — harness denial.** The SDK sub-agent's own harness denied `uv run python scripts/execute_evals.py` despite bypass mode (R06 L954–968). *Fix:* explicit allow entries in the shipped `settings.json` for the runner and launcher invocations; verify against a case dir's merged deny rules, since the exact matching rule was never pinned.
- **D04 — killed by teardown; fragile launch.** A shared **detached launcher** (`scripts/launch_detached.py`, name provisional) used by the Diagnostician for *every* sub-agent: request JSON persisted under `investigations/<name>/requests/`, launched under `setsid` with absolute paths, stdout/stderr to `artefacts/<role>/`, a receipt (pid, paths, timestamp) returned, a `status` subcommand. The pattern reverse-engineered in R02 (L374–381) and R06 (L179–181), made first-class.
- **D28 — output-path collisions.** The launcher names every output per invocation (`report_<iter>_<role>_<n>.md`), never per role.
- **D36 (guard half) — zero-sample conditions.** The runner's status/report flags any condition-model pair that completed zero samples, prominently, as a distinct line; the doc half is WP7.
- **D12 (mechanism half) — spend telemetry.** `runner.py` writes a `cost.json` sidecar per sub-agent invocation from the SDK result message's cost/usage fields; the runner records per-command token usage from the eval log header. A tiny `scripts/spend.py` sums both into a running total. The stopping *policy* is WP5-A.
- **D14 (mechanism half) — turn cap.** `subagent_max_turns` input field threaded through `runner.py` (default unchanged at 100); the work-order *policy* is WP3.

*Effort:* large. *Verify:* offline — unit tests for metrics stripping, zero-sample flag, report assembly from a fixture status file, launcher receipt/status round-trip; live (gated on budget) — one 2-condition tiny eval end-to-end through the Executor agent, plus one long (>10 min) Docker eval completing detached. **Lands first; WP2 and WP6 rebase onto it.**

---

## WP2 — Sub-agent routing & credentials (P1)

*A correlated credential cap took down all sub-agents in five of six runs — and, in R05, the target model too.* Owns the route resolver in `runner.py` and every statement in the docs that says no direct-OpenAI route exists.

- **D05 — two routes only.** *Fix:* a third route so the pipeline degrades to a live route instead of stopping. **This is not a config change.** The Claude Code CLI the SDK spawns speaks only the Anthropic Messages API, so reaching OpenAI (or any OpenAI-compatible endpoint) needs a translating proxy — LiteLLM's `/v1/messages` passthrough is the known pattern. Design: `subagent_route: "openai"` expects (or starts, if simpler) a local proxy and points the CLI at it with `OPENAI_API_KEY`. **Spike first** (≤1 hour): confirm a sub-agent completes a trivial task through the proxy; if it cannot, fall back to documented key redundancy — Claude sub-agents via OpenRouter's Anthropic endpoint (R02 did this, with a continuity check) — and record why. Either way, fix the error text at the cross-family gate, `critic_interface_contract.md §Cross-family routing`, `SKILL.md`, README, and `subagent_invocation.md` (five places today). Cross-reference the degraded-mode policy (WP5-B, D17) for the failover procedure; failover is a recorded, per-role, opt-in decision, never silent.
- **D27 — non-Claude ergonomics.** Set `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT` and an explicit `CLAUDE_CODE_MAX_CONTEXT_TOKENS` in the route env by default for non-Claude routes; document the benign `unrecognized_model` warning in `subagent_invocation.md`.

*Effort:* medium. *Verify:* offline route-resolution unit tests (route chosen given keys present/absent; env vars set per route); the spike is the live test. *Coordinate:* `runner.py` shared with WP1 — rebase after WP1.

---

## WP3 — Analyst instruments & measurement validity (P0)

*The primary and auxiliary instruments failed or went unvalidated in most runs; two of these threaten the validity of reported rates.* Owns the addendum, the Analyst sub-agent, the Diagnostician's Analyst guide, and the named 3c/3d bullets.

- **D08 — validated grader on the wrong span.** Require the report to state extraction provenance mechanically ("graded artefact = last fenced block of last assistant message; N of M messages read") plus one hand-verifiable extraction exhibit per condition. R04's rebuilt-grader request is the template.
- **D25 — precision drifts per condition.** Require a per-condition precision estimate (a small hand-read audit sample of positives per new condition population) whenever the loaded grader is applied to a new condition; flag non-conservative drift on the Summary line.
- **D06 — eval-awareness scanners fail validation.** Re-operationalise from R06 iter-1's wording (validated at κ=0.767) *and* gate: if the awareness scanner cannot validate, the `### Eval-awareness split` is emitted as "unmeasurable / hand-read", and the Diagnostician marks eval-awareness candidates untestable up front rather than carrying noise.
- **D07 — manipulation checks fail / pin at ceiling.** Construct guidance against near-constant or near-tautological checks (R05's φ analysis), a validation threshold before a row is scorable, and a deterministic-proxy fallback labelled `[provisional]` used only to void rows (R04's pattern).
- **D30 — combined-blind delivery.** Make R02's shape the contract default: all of a round's construal checks sent as one de-identified set, each validated, each rated across every condition, verdicts derived caller-side from the private mapping. **This is a five-place interface change** (addendum → `subagent_invocation.md §Forensic mode` → forensic system prompt → `agent.py` prompt builder → the 3c bullet); check the prompt builder first — an unread key is silently inert.
- **D14 (policy half) — turn budget spent on the wrong work.** Bake the work order into the contract and prompt (grader → manipulation checks → open-ended scan) and a split protocol for large scanner×condition workloads. Pair with WP1's `subagent_max_turns`.
- **D15 — harvest not done.** Make the open-ended-scan harvest an explicit checked deliverable of 3d step 7 with a recorded "nothing harvested" line.
- **D16 (scanning-model half).** A target-family note for `scanning_model` and Analyst selection with the mitigations stated (hand-label validation; near-deterministic grading on action-mediated behaviour). The Diagnostician-family half is WP5-B.

*Effort:* large. *Verify:* offline, no spend — **the persisted grader packages are complete units**: e.g. `case-6-.../artefacts/analyst/grader/{scanner.py,metrics.json,validation_set.csv,README.md}` with `scanner_definition_sha256`, and case-5's equivalent. Re-run them over their recorded transcripts and confirm the recorded rates reproduce; this is the regression harness. Unit-test the provenance and precision additions on those transcripts. *Split option:* addendum/guide edits vs sub-agent prompt edits.

---

## WP4 — Critic interface & candidate handoff (P2)

*The Critic's content consistently earned its cost; its I/O and the candidate input it receives lagged.* The fixes are two deterministic tools plus contract wording.

- **D38 — prose candidates serialised empty.** No serialiser exists in the repo: R01 wrote `scripts/ledger_to_json.py` (same latent bug — a prose candidate yields empty strings with no warning); R05 serialised by hand and shipped `C10` with empty `statement`/`discriminating_signature` to both the Designer and the Critic. *Fix:* ship a sanctioned `scripts/ledger_to_json.py` that parses `CANDIDATES.md` per the §State Management format and **fails loudly** on any live candidate missing a *Statement* or *Discriminating signature* field. Unit-test on a prose-candidate fixture. Do this first.
- **D33 / D09 — missing signatures, non-verbatim headings.** Ship `scripts/check_report.py <role> <report.md>`: verifies verbatim headings for the role's active mode and, for the Critic, that every proposed candidate carries a signature. Caller re-asks once on failure. Also ship the verbatim-headings checklist in the Critic request (as Mapper/Designer already do).
- **D10 — contract path unresolvable.** Inject the contract's absolute path (resolved from the scaffold root in `agent.py`) into the Critic's prompt instead of a cwd-relative path. Note the generalisation to all six sub-agents as a Wave 3 item.
- **D11 — re-proposing ledger candidates.** Remind in the request; dedup by signature caller-side on merge (pairs with WP5-A's merge-by-signature).

*Effort:* small–medium. *Verify:* offline — serialiser tests on R01's and R05's `CANDIDATES.md`; `check_report.py` on saved Critic reports from the case dirs (R04's Gemini reports are the non-conformant fixtures).

---

## WP5 — Diagnostician governance, budget & ledger economics (P1)

*Mostly additive policy in one doc (`diagnostician_responsibilities.md`), so one session owns it — as two sequential halves (A then B) on one branch, not in parallel; the halves share 3d and the Anti-Patterns list.*

**WP5-A — cost & speed:**
- **D29 — ledger outruns elimination.** Ledger admission/triage: a rank-gated testing tier so only leading candidates carry full prediction rows; stricter merge-by-signature at Critic time; an inertness sweep. Bounds prediction-table width, which drives Critic, derivation and Analyst cost.
- **D13 / D37 — budget on the wrong axis; illusory determinism.** Recalibrate the default budget table for this mode's economics (sub-agents/scanners dominate, evals are cheap: favour n over breadth); **require a noise-floor characterisation** (the identical condition repeated k times) before any sub-large effect is trusted; warn that `seed`/`temperature` determinism can be a no-op (Anthropic endpoints).
- **D12 (policy half).** A sequential-stopping rule keyed to the running spend total from WP1's `spend.py`, so the budget exit fires as a decision.
- **D31.** One line on when a second cross-family Critic is worth running (default one; a second only when dangerous/leading candidates are contested).

**WP5-B — governance, firewall policy, Phase 4:**
- **D17 — degraded-mode policy.** Which components may be substituted, which force a pause, how to record the trade; resolves "pause and escalate" vs "run autonomously". References WP2's failover route and WP1's sanctioned direct-runner fallback.
- **D18 / D19 — Phase 4's purpose and timing.** State what predict-to-test is *for* (a failed prediction is informative — R03, R05 exemplars), when it is warranted vs another Phase 3 round, and a Critic-independent factor-selection fallback.
- **D35 — must-exclude candidate.** A clean test (or an explicit "carried untested-dangerous") becomes a tracked termination precondition in the stopping rule and the Phase 5 report.
- **D34 — content invariance.** For register/framing variants, require a severity/intensity check on *retained* content, not only presence/placement.
- **D20.** When purpose-aware re-reading of a blind Analyst verdict is legitimate vs an override.
- **D21.** The chained-variant comparator convention and when it is valid.
- **D16 (Diagnostician half).** A Phase 0 note when the Diagnostician shares the target's family, with mitigations.

*Effort:* large but prose. *Verify:* fork/second-agent review against the D-IDs; `spend.py` is WP1's to test.

---

## WP6 — Blinding firewall (P0)

*The single most serious firewall defect: R02's Analyst blinding was silently defeated.* Not a thin staging step: the real condition name reaches the Analyst through **three** channels today — the log directory name, the `--metadata condition=` / `--tags cond:` values embedded in every `.eval` log, and the variant's environment directory path recorded in the log's eval spec.

- **D32 — opaque at source, plus a leak check.** (i) The Diagnostician assigns an opaque execution id to every arm at 3a (`c_<4 hex>`), names the variant's *environment copy* by that id, and passes only opaque ids to the Executor, so no descriptive name enters anything Inspect records. The private map (`CONDITION_LABEL_MAP_iter<N>.json`, R06's shape) lives in the investigation dir and is added to the Analyst's `RESTRICTED_FILES`. Analyst-time labels are still re-randomised per invocation on top. (ii) `scripts/check_blinding.py <label_map> <transcript_source.json>` greps every path, filename, and `.eval` zip member for every real name in the map and exits non-zero on any hit; the Diagnostician runs it before every Analyst invocation. (iii) A Designer-contract rule: semantically descriptive variant names must not reach the Analyst via any channel — content, path, or metadata. R02's neutrally-named staging directories remain the documented fallback for logs produced before the ids existed.

*Effort:* small–medium. *Verify:* offline — run the leak check on R02's iteration-1 layout (must fail) and on an opaque-id layout (must pass). *Coordinate:* after WP1 (runner accepts arbitrary condition ids), WP3 (Analyst `agent.py`), WP5 (3a/3c wording).

---

## WP7 — References & logging hygiene (P2)

- **D26 — `.eval` logs are zip archives.** Ship `scripts/dump_transcripts.py` (R01's `dump_transcript.py` is the exemplar) producing plain-text per-sample dumps, and mandate passing dumps by path to any log-reading sub-agent (Mapper, Critic); note it in `subagent_invocation.md`.
- **D22 — cwd-relative resolution.** Document Inspect's cwd-relative resolution of the compose file and `Sample.files`; recommend per-condition `experiment_dir` invocation.
- **D36 (doc half).** Document that parameterised AM-style variants change their own sample id, so a `--sample-id` pin can select nothing; point at WP1's zero-sample flag.
- **D23 — log ordering/duplication.** Log-template discipline plus a lint pass (a small `scripts/lint_log.py` for duplicate headings and stranded blocks is fine if cheap).

*Effort:* small. *Verify:* offline — `dump_transcripts.py` over the case dirs' 175 logs; the rest is doc review.

---

## Coverage check

All 38 deficiencies are assigned; where a deficiency is split into a mechanism half and a policy half, both owners are named:

- **WP1**: D01, D02, D03, D04, D24, D28, D36 (guard), D12 (mechanism), D14 (mechanism), + the engine-level scorer leak
- **WP2**: D05, D27
- **WP3**: D06, D07, D08, D14 (policy), D15, D16 (scanning-model), D25, D30
- **WP4**: D09, D10, D11, D33, D38
- **WP5**: D12 (policy), D13, D16 (Diagnostician), D17, D18, D19, D20, D21, D29, D31, D34, D35, D37
- **WP6**: D32
- **WP7**: D22, D23, D26, D36 (doc)

**Launch order** (canonical; the briefs file restates it): Wave 0 — this document lands in the repo and the evidence is preserved. Wave 1 — **WP1** (interactive session; live runs) in parallel with **WP3, WP4, WP5 (A→B), WP7** (offline-testable). Wave 2, after WP1 merges — **WP2, WP6**. Wave 3 — propagation pass over the mechanical surfaces and the D10 generalisation, one cheap end-to-end smoke run, workflow memory update.
