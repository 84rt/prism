# REVIEW-WP7 — References & logging hygiene

Worktree `/workspaces/prism-wp7`, branch `wp7`. Nothing committed or staged; no file under `/workspaces/forensics-cases/` touched; no API spend.

## D-IDs

| ID | Status | Where |
|---|---|---|
| D26 | **closed** | `scripts/dump_transcripts.py` (new); `subagent_invocation.md §Transcript handoff` (new `###` subsection after `§subagent_route`, before `## Environment Explorer`) mandates dump paths for any log-reading sub-agent, names the Analyst as the one exception; one-line pointers on the Mapper `reference_transcript` and Critic `transcript_paths` bullets; gotcha 13 in `inspect_reference.md` |
| D22 | **closed** | `inspect_reference.md §Common gotchas` item 11: cwd-relative compose / `Sample.files` resolution, the visible and the silent failure shapes, per-condition `experiment_dir` rule, smoke-test-one-sample advice |
| D36 (doc) | **closed** | `inspect_reference.md §Common gotchas` item 12: AM-style id-from-parameters, re-derive the pin per `-T` condition, points at the batch runner's zero-sample flag (WP1) as the guard — "always an execution failure, never a rate" |
| D23 | **closed** | Template comment at the top of the `INVESTIGATION-LOG.md` template in `diagnostician_responsibilities.md §State Management` (the only edit to that doc); `scripts/lint_log.py` (new): duplicate-heading (scoped to parent, so per-iteration template repeats are not flagged), verbatim duplicate-block, stranded iteration-N block |

## Files changed

New: `scripts/dump_transcripts.py`, `scripts/lint_log.py`, `tests/test_dump_transcripts.py`, `tests/test_lint_log.py`, this file.
Edited: `.claude/docs/references/inspect_reference.md` (+3 gotchas), `.claude/docs/investigator/subagent_invocation.md` (+1 subsection, 2 bullets amended), `.claude/docs/investigator/diagnostician_responsibilities.md` (+9-line template comment), `install.sh`, `uninstall.sh`, `README.md`, `.claude/settings.json`.

## Propagation checklist (five surfaces, both scripts)

- [x] `install.sh` `link_file` list
- [x] `install.sh` `GITIGNORE_ENTRIES`
- [x] `uninstall.sh`
- [x] README file tree
- [x] `.claude/settings.json` allow: `Bash(uv run python scripts/dump_transcripts.py *)`, `Bash(uv run --no-project python scripts/lint_log.py *)`

No sub-agent contract, request field, prompt builder or route changed, so the pipeline-spine walk is a no-op for runtime: `agent.py` files untouched. Doc spine: Diagnostician doc → invocation reference → (Mapper contract already accepts "a text transcript"; Critic contract's `transcript_paths` is path-typed) — see judgement call 3.

## Tests and verification (all offline)

`uv run --with "inspect_ai==0.3.263" --with "inspect-scout==0.5.2" --with claude-agent-sdk --with python-dotenv --with pytest pytest tests/ -v` → **29 passed in 6.93s** (19 dumper, 10 linter; 8 of them run against the corpus and skip if it is absent).

Dumper over the six case dirs (scratch output, not kept): `183/183 logs dumped, 7666 sample files`, 183 leaf directories, zero stderr. Independent count via `read_eval_log_sample_summaries` over the same glob: 183 logs / 7666 samples. The brief's "175" is the `find`/`rglob` count, which skips R02's eight `staged/set_X -> logs/vN` directory symlinks; the dumper follows directory symlinks deliberately (see judgement call 2), and a run before that change gave exactly 175/175.

Spot-checks against `read_eval_log` (three families): case-1 reference episode (GPT-5, 66 messages → 66 rendered headers, 32/32 tool calls, 17 `<reasoning-summary>` blocks, no `### SCORE`); case-3 DeepSeek-R1 log (30/30 samples, raw `<reasoning>` rendered, last assistant text present); case-5 Claude log (20/20 samples, last assistant text present, no scores).

Linter on the D23 fixtures — exactly the findings the analysis recorded and nothing else: R01 → duplicate-heading + duplicate-block at L1403/1431/1465 (the second Phase 4 copy); R06 → duplicate-heading at L157–158 and stranded-block at L1422 (`iteration 1` checks under Phase 4). Both pinned as tests.

Defect found and fixed during verification: the first dumper draft keyed output directories on the log *stem*, and 29 of 175 logs were copies sharing a stem (reference episodes under `artefacts/reference/`, quarantined re-runs) — later logs silently overwrote earlier ones (146 dirs / 5937 files on disk vs 7186 reported). Output now mirrors each log's path relative to its input directory, and two logs that would still share a directory are refused with exit 1 rather than merged. Tested.

## Code review

Independent reviewer subagent (Sonnet) against the four D-IDs and the changed files: no blocking bugs; blackout default, template-comment-only edit, and five-surface completeness confirmed; the only flag is the two pointer bullets in the Mapper/Critic sections (judgement call 4 below). Verdict: ready to merge.

## Open judgement calls for the owner

1. **Scores off by default in the dumper.** The exemplar emitted scores unconditionally; I inverted that (`--with-scores` opt-in) because dumps are made *before* the Analyst reports (Critic briefing at step 2 / 3d) and a default-on score line is a blackout leak surface. The doc says when the flag is legitimate. If you prefer the exemplar's behaviour, it is one default.
2. **Directory symlinks are followed** (`os.walk(followlinks=True)`), so a blinded `staged/condition_A -> logs/v3` layout dumps under the opaque name. Cost: a symlink cycle would loop; none exist in the corpus and none are plausible under `iterations/`. Flagging in case WP6's staging design prefers copies.
3. **Contracts and system prompts not touched.** The Mapper contract already says "`.eval` log, or a text transcript"; the Critic contract (WP4-owned) says `transcript_paths` without a format. The mandate lives in the invocation reference and the Diagnostician is the only caller, so runtime is covered; a one-line "expect plain text" in `critic_interface_contract.md §candidate_critique` would belong to WP4/Wave 3.
4. **Two one-line pointers in the Mapper and Critic sections of `subagent_invocation.md`** (unowned by any WP per the coordination table) — hunk-disjoint with WP1/WP2/WP3's sections, but they are edits outside the strictly named "new subsection". Revert if you want the diff minimal.
5. **`settings.json` entries** sit next to the `openrouter_endpoints.py` allow line; WP1 adds its own nearby, so expect a trivial merge conflict there.
6. **Zero-sample flag wording (gotcha 12)** describes WP1's flag as "a distinct line in its status file and in the Executor's report" per the fixes doc; reconcile the exact name in the Wave 3 pass once WP1 lands.
7. **Linter is advisory** (exit 1 on findings; the template comment says run it before each Decision block). No hook wires it in — deliberate, since the log is append-only and the fix is a forward pointer, not an edit.
