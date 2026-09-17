# Model Forensics Fixes — per-session launch briefs

One session per work-package (WP5 is one session running A then B). Each session's opening prompt is: **the Shared Preamble + one WP brief** (paste both, or point the session at this file and name the WP). Work-package definitions, per-deficiency fixes, and the coordination table live in `2026-09-17-model-forensics-fixes.md`; the evidence for each deficiency is in `2026-09-17-model-forensics-analysis.md` (repo copy of ANALYSIS.md, links resolve against `/workspaces/forensics-cases/`).

**Launch order.** Wave 1: **WP1** (interactive terminal session — it needs live runs and budget calls) in parallel with **WP3, WP4, WP5, WP7** (offline-testable; run as worktree sub-sessions). Wave 2, after WP1 is reviewed and merged: **WP2, WP6**. Wave 3: a propagation pass (install/gitignore/uninstall/README/settings for every new file; the D10 contract-path generalisation to all six sub-agents), one cheap end-to-end smoke run, and the workflow memory update.

---

## Shared Preamble (prepend to every WP session)

> You are implementing **one work-package** of a scaffold-improvement effort for Prism's Model Forensics mode (`/diagnose`). Before editing anything, read: the *Design principle* section and your WP's section of `docs/superpowers/plans/2026-09-17-model-forensics-fixes.md`, and the entries in `docs/superpowers/plans/2026-09-17-model-forensics-analysis.md` for the deficiency IDs (`D…`) your WP owns. This is a *scaffold-editing* session, not a `/diagnose` run: read the prism repo, the analysis, the fixes doc, and the Prism workflow memories at `~/.claude/projects/prism/memory/MEMORY.md` freely (start with `feedback_scaffold_change_propagation.md` and `project_model_forensics_mode_complete.md`).
>
> **Repo and workspace.** The scaffold source is the `prism` repo — specs under `.claude/docs/{contracts,investigator,references}/`, skills under `.claude/skills/diagnose/`, sub-agents and `runner.py` under `subagents/`, scripts under `scripts/`. You are working in a dedicated git worktree for this WP (`/workspaces/prism-wp<N>`); if you are not, say so and stop. **Do not commit, stage, branch, or push** — leave every change uncommitted in the worktree for the owner to review and commit by hand.
>
> **Boundaries.** Touch only the files and paragraphs this WP owns (fixes-doc coordination table) and only the deficiencies it owns. Make the minimal change that closes each deficiency; record open judgement calls rather than expanding scope. If you must touch a coordination-hotspot file outside your ownership, stop and flag it in your review note instead.
>
> **Invariants — must not regress (add both to your definition of done):** (1) the **score blackout** — no scorer output reaches the Diagnostician before the Analyst reports; (2) the **immutable pre-registration table + deferred scoring** property. Any change to the Analyst, Executor, runner, or execution scripts is where these silently break.
>
> **Propagation.** If you change a sub-agent contract, prompt, request field, route, or serialiser, propagate by walking the pipeline spine (Diagnostician doc → request builder in `agent.py` → sub-agent system prompt → report consumer), **not** by grepping — the `**data` prompt builder discards unread keys silently (see the propagation memory). Any new file is a five-surface change: `install.sh` `link_file` list, `GITIGNORE_ENTRIES`, `uninstall.sh`, README file tree, and `.claude/settings.json` if anything runs it.
>
> **Test environment.** The prism repo has no `pyproject.toml`, `tests/` or pytest setup of its own; sub-agents pull their dependencies per invocation (`uv run --with claude-agent-sdk --with python-dotenv …`). Put new tests under `tests/` (they are not installed into targets, so the five-surface rule does not apply to them) and run them self-contained, matching the versions the six runs used: `uv run --with "inspect_ai==0.3.263" --with "inspect-scout==0.5.2" --with claude-agent-sdk --with python-dotenv --with pytest pytest tests/`. The case dirs' `.venv` symlinks all resolve to `/workspaces/forensics-sweep/.venv` (Inspect 0.3.263, Scout 0.5.2, no pytest, no SDK) — read from it, do not install into it.
>
> **Verify before claiming done.** Prefer offline tests against the on-disk artefacts in `/workspaces/forensics-cases/case-*/` (persisted graders with hashes, `.eval` logs, request payloads, label maps, saved sub-agent reports) — no API spend. Only WP1 and WP2's spike need a live call; use the smallest fixture and gate it on the budget note in your brief. Show test output; do not self-assert (`superpowers:verification-before-completion`). Finish by running `superpowers:requesting-code-review` against the D-IDs you were meant to close, and write `REVIEW-WP<N>.md` at the worktree root: D-IDs closed (and any not closed, with why), files changed, tests run with their output, the propagation checklist ticked, and open judgement calls for the owner.

---

## WP1 — Execution layer & launch infrastructure (P0, Wave 1, interactive)

**Goal.** Make execution work end to end without the lead investigator ever routing around it: a deterministic batch runner does the mechanical work, the Executor agent keeps the judgement, and every sub-agent launches detached.
**Owns.** `scripts/execute_evals.py` (grows into the batch runner), a new detached launcher (`scripts/launch_detached.py`, name yours to settle), `subagents/experiment_executor/*`, `.claude/docs/contracts/executor_interface_contract.md`, `subagents/runner.py` **only** for the cost sidecar and `subagent_max_turns`, `.claude/settings.json` allow entries, `subagent_invocation.md §General Pattern` (launcher), `inspect_reference.md §Concurrency heuristics` (ceiling), the 3b paragraph of `diagnostician_responsibilities.md`.
**Reads (exemplars of the fix already working).** `case-1-coding-agent/scripts/run_condition.py` (score stripping to `.private.json`, transcript-shape report), `case-6-.../investigations/shutdown_bypass/run_evals_blind.sh` + `status.sh` (spec construction, quarantine-by-redirect, polling), case-2's log L374–381 and case-6's L179–181 (the detached launch pattern), case-6 `requests/` (durable request store and per-invocation naming).
**Deficiencies & done-when:**
- **D01/D24** — runner takes the contract request JSON, builds commands, preflights, launches detached under `setsid` with a status file, emits a draft report in the contract's shape; Executor protocol becomes launch + report with polling. Done: a >10 min Docker eval completes detached and the Executor returns a conformant, score-free report.
- **Engine scorer leak** — `verify_eval_log`'s `metrics` never reach stdout or the status file (sidecar only). Done: unit test with a scored fixture log.
- **D02** — default ceiling `max_parallel: 1`, `max_connections: 4`, overridable via `overrides`; agent raises only with recorded rationale.
- **D03** — allow entries for the runner and launcher; verified against a case dir's merged settings (copy one case's `.claude/settings.json` deny rules into a scratch target and run the Executor there).
- **D04** — launcher: persistent `investigations/<name>/requests/`, `setsid`, absolute paths, receipt + `status`; used for all sub-agents; documented in `subagent_invocation.md §General Pattern`.
- **D28** — per-invocation output names from the launcher.
- **D36 (guard)** — zero-sample condition-model pairs flagged as a distinct line in status and report.
- **D12 (mechanism)** — `cost.json` per sub-agent invocation from the SDK result; per-command eval usage from the log header; `scripts/spend.py` sums. Policy is WP5-A's.
- **D14 (mechanism)** — `subagent_max_turns` threaded through `runner.py`, default 100.
**Verify.** Offline: the unit tests above plus report assembly from a fixture status file. Live (gate on budget; announce spend before running): a 2-condition × 5-epoch run on the cheapest case (the deepseek-r1 blackmail case is ~$0.01/sample) through the Executor agent; then one long sandboxed eval (a single `reward_hacking` sample, case 1) detached. **Blocks WP2 and WP6 — land first.**

---

## WP2 — Sub-agent routing & credentials (P1, Wave 2, after WP1)

**Goal.** Survive a single credential cap instead of losing the whole specialist layer.
**Owns.** The route resolver in `subagents/runner.py`; `subagent_invocation.md §subagent_route`; every "no direct-OpenAI route" sentence (`diagnostician_responsibilities.md §Cross-family Critic gate`, `critic_interface_contract.md §Cross-family routing`, `.claude/skills/diagnose/SKILL.md`, README §Prerequisites, `subagent_invocation.md`).
**Deficiencies & done-when:**
- **D05** — a third route. The CLI speaks only the Anthropic Messages API, so this needs a translating proxy (LiteLLM `/v1/messages` passthrough). **Spike first, ≤1 hour, minimal spend:** one sub-agent completes a trivial task via the proxy on `OPENAI_API_KEY`. If it works: `subagent_route: "openai"` with the proxy documented (or started by the runner if simpler), and the five error-text sites corrected. If it doesn't: document key redundancy instead (Claude sub-agents via OpenRouter's Anthropic endpoint with a continuity check — R02 L562–598), correct the five sites to say what *is* available, and record the spike result. Failover is a recorded per-role decision, cross-referenced to WP5-B's degraded-mode policy — never silent.
- **D27** — set `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT` and an explicit `CLAUDE_CODE_MAX_CONTEXT_TOKENS` in the route env for non-Claude routes; document the benign `unrecognized_model` warning.
**Verify.** Offline route-resolution unit tests (route chosen and env vars set given keys present/absent). **Coordinate:** `runner.py` shared with WP1 — rebase onto merged WP1 before starting.

---

## WP3 — Analyst instruments & measurement validity (P0, Wave 1)

**Goal.** Close the ways the primary and auxiliary instruments failed or went unvalidated. **Best offline-testable WP** — the persisted grader packages on disk are complete regression units.
**Owns.** `.claude/docs/contracts/analyst_forensic_addendum.md`, `subagents/transcript_analyst/*`, `.claude/docs/investigator/diagnostician_analyst_guide.md`, `subagent_invocation.md §Forensic mode`, and in `diagnostician_responsibilities.md` **only** the 3c "one invocation per check" bullet and the 3d step-7 harvest wording. (Splittable: doc edits vs sub-agent-prompt edits.)
**Deficiencies & done-when:**
- **D08** — extraction provenance stated mechanically in the report + one hand-verifiable extraction exhibit per condition (R04's rebuilt-grader request is the template).
- **D25** — per-condition precision from a small hand-read audit sample whenever the loaded grader meets a new condition population; non-conservative drift flagged on the Summary line.
- **D06** — re-operationalise the awareness scanner from R06 iter-1's wording (κ=0.767) *and* gate: unvalidatable → "unmeasurable / hand-read" split and eval-awareness candidates marked untestable up front.
- **D07** — construct guidance against near-constant/near-tautological checks (R05's φ analysis), validation threshold before a row is scorable, `[provisional]` deterministic-proxy fallback used only to void (R04).
- **D30** — combined-blind delivery as the default: one de-identified check set, each validated, rated across all conditions, verdicts derived caller-side. **Five-place change** — addendum, invocation reference, forensic system prompt, `agent.py` prompt builder (check this first), the 3c bullet. Verify by capturing the built prompt, not by reading the diff.
- **D14 (policy)** — work order grader → checks → open-ended scan baked into contract and prompt; a split protocol for large scanner×condition workloads (pairs with WP1's `subagent_max_turns`).
- **D15** — harvest is an explicit checked deliverable of 3d step 7, with a recorded "nothing harvested" line.
- **D16 (scanning-model half)** — target-family note for `scanning_model`/Analyst selection with stated mitigations.
**Verify.** Offline, no spend: re-run the persisted graders (`case-6-.../artefacts/analyst/grader/`, `case-5-.../artefacts/analyst/grader/`, case-1's `grader/` — each with recorded hashes, validation sets and metrics) over their recorded transcripts and confirm the recorded rates reproduce; that is your regression harness. Unit-test the provenance and precision additions on those transcripts.

---

## WP4 — Critic interface & candidate handoff (P2, Wave 1)

**Goal.** Two deterministic tools plus contract wording. Content is fine; plumbing isn't.
**Owns.** `.claude/docs/contracts/critic_interface_contract.md` (except the cross-family sentence WP2 owns), `subagents/redteam_critic/*`, new `scripts/ledger_to_json.py`, new `scripts/check_report.py`.
**Deficiencies & done-when:**
- **D38** — sanctioned `scripts/ledger_to_json.py`: parses `CANDIDATES.md` per §State Management, emits the `candidates` array the Designer and Critic take, **fails loudly** on a live candidate missing *Statement* or *Discriminating signature*. Exemplar: `case-1-.../scripts/ledger_to_json.py` (has the latent bug — do not copy it uncritically); failing fixture: `case-5-.../CANDIDATES.md` (C10 is prose; see `artefacts/designer/iter2_request.json` L104–109 for the empty output it produced). Do this first.
- **D33 / D09** — `scripts/check_report.py <role> <report.md>`: verbatim headings for the active mode; for the Critic, every proposed candidate carries a signature; non-zero exit lists the defects. Contract and request get the verbatim-headings checklist and a one-re-ask rule. Fixtures: R04's Gemini reports (non-conformant), R06's Grok iter-3 report (non-verbatim headings), R03's gpt-5.1 reports (conformant).
- **D10** — absolute contract path, resolved from the scaffold root in `agent.py`, injected into the Critic prompt. Note the all-six-agents generalisation for Wave 3.
- **D11** — request reminder + caller-side dedup by signature on merge.
**Verify.** Offline: both scripts' unit tests on the fixtures above; propagation walk for any request-field change.

---

## WP5 — Diagnostician governance, budget & ledger economics (P1, Wave 1, one session, A then B)

**Goal.** The cost/speed levers (A) and the governance/firewall/Phase-4 guidance (B), in the one doc, sequentially on one branch. Finish A's edits and its review note before starting B.
**Owns.** `.claude/docs/investigator/diagnostician_responsibilities.md` as sole writer **except** the paragraphs the coordination table assigns elsewhere (leave untouched: §Cross-family gate error text; the 3c one-invocation-per-check bullet and 3d step 7; the 3a apply-diffs/label-map and 3c opaque-labels bullets; the 3b Executor paragraph). Also `.claude/skills/diagnose/candidate-methodology.md` and the Autonomy Agreement section of `SKILL.md` where a new agreed parameter is needed.
**A — deficiencies & done-when:**
- **D29** — ledger admission/triage: rank-gated testing tier (only leading candidates carry full prediction rows), stricter merge-by-signature at Critic time, an inertness sweep; §State Management template updated to carry the tier.
- **D13/D37** — recalibrated default budget table (favour n over breadth; sub-agents dominate); **required noise-floor characterisation** (identical condition × k) before trusting any sub-large effect; determinism warning (`seed`/`temperature` no-ops on Anthropic endpoints).
- **D12 (policy)** — sequential-stopping rule keyed to WP1's `spend.py` running total; budget exit fires as a decision and is recorded in the log.
- **D31** — one-line rule for a second Critic.
**B — deficiencies & done-when:**
- **D17** — degraded-mode policy: substitutable components, pause-forcing ones, how to record the trade; cites WP2's failover route and WP1's sanctioned direct-runner fallback.
- **D18/D19** — what Phase 4 is *for* (R03/R05 exemplars), when it beats another Phase 3 round, Critic-independent factor-selection fallback.
- **D35** — clean test of the must-exclude candidate (or explicit "carried untested-dangerous") as a tracked termination precondition in the stopping rule and the Phase 5 report.
- **D34** — content-invariance check on severity/intensity of retained content for register/framing variants.
- **D20** — legitimate purpose-aware re-reading vs override, one rule.
- **D21** — chained-variant comparator convention.
- **D16 (Diagnostician half)** — Phase 0 note for a Diagnostician sharing the target's family, with mitigations.
**Verify.** Doc-only → fork/second-agent review against the D-IDs, separately for A and B. If a template changes, check it against R06's `CANDIDATES.md` and log as a worked example.

---

## WP6 — Blinding firewall (P0, Wave 2, after WP1, WP3, WP5)

**Goal.** Close the most serious firewall defect: the Analyst learning conditions from paths and metadata. Three channels leak today — log directory names, `--metadata condition=` / `--tags cond:` inside every `.eval`, and the variant environment path in the log spec.
**Owns.** New `scripts/check_blinding.py`; a rule in `.claude/docs/contracts/designer_interface_contract.md`; the 3a apply-diffs/label-map and 3c opaque-labels bullets of `diagnostician_responsibilities.md`; one `RESTRICTED_FILES` entry in `subagents/transcript_analyst/agent.py`.
**Deficiencies & done-when:**
- **D32** — (i) opaque execution ids assigned at 3a (`c_<4 hex>`), used for environment-copy directory names and passed as the only condition names to the Executor, so nothing Inspect records carries a descriptive name; private map `CONDITION_LABEL_MAP_iter<N>.json` (R06's shape) in the investigation dir, denied to the Analyst; Analyst-time labels still re-randomised on top. (ii) `check_blinding.py <label_map> <transcript_source.json>` greps every path, filename and `.eval` zip member for every real name and exits non-zero on a hit; run before every Analyst invocation. (iii) Designer-contract rule: descriptive names must not reach the Analyst via content, path, or metadata. R02's neutrally-named staging dirs documented as the fallback for pre-existing logs. Done: the check passes on an opaque-id layout and fails on R02's iteration-1 layout.
**Verify.** Offline: the two runs of the leak check above (`case-2-.../iterations/01*/logs/` is the failing fixture). **Coordinate:** rebase after WP1, WP3 and WP5 merge.

---

## WP7 — References & logging hygiene (P2, Wave 1)

**Goal.** Carry the Inspect/eval-interaction gotchas the references miss, ship the transcript dumper, tidy the log template.
**Owns.** `.claude/docs/references/inspect_reference.md §Common gotchas`, the transcript-handoff note in `subagent_invocation.md` (new subsection, not §General Pattern or §routes), new `scripts/dump_transcripts.py`, the log template's discipline note in §State Management (coordinate wording with WP5 by keeping it to the template comment) and an optional `scripts/lint_log.py`.
**Deficiencies & done-when:**
- **D26** — `dump_transcripts.py` (exemplar: `case-1-.../scripts/dump_transcript.py`) writes plain-text per-sample dumps from `.eval` logs; the invocation reference mandates passing dumps by path to any log-reading sub-agent.
- **D22** — document cwd-relative resolution of the compose file and `Sample.files`; recommend per-condition `experiment_dir` invocation.
- **D36 (doc half)** — document the AM-style sample-id shift; point at WP1's zero-sample flag.
- **D23** — log-template discipline note; `lint_log.py` for duplicate headings and stranded blocks if cheap (R01's log has the duplicated Phase 4 block as the fixture).
**Verify.** Offline: the dumper over the case dirs' 175 logs (count in = count out, spot-check three); the linter on R01's and R06's logs; the rest is doc review.
