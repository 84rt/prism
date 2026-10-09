# REVIEW — WP6 (revised 2026-10-08): Analyst blinding firewall, both modes

Worktree `/workspaces/prism-wp6`, branch `wp6` (base `44316d8`). Everything is uncommitted; nothing staged.

## What was done

- **Opaque at source, both modes.** The lead agent (Diagnostician at 3a / 1b; Orchestrator at Step 2b–2d) gives every condition, controls included, a random opaque id `c_<4 hex>`. That id names the condition's environment copy and condition-specific files, and it is the only name the Executor sees. Since the runner writes the condition name into `logs/<name>/`, `cond:` and `metadata.condition`, no descriptive name reaches anything Inspect records. Iteration directories and `experiment_name` (which becomes the `exp:` tag) are now neutral as well: Orchestrator `iter_01` instead of `explicit_goal_framing_v1`; resample `iterations/00_round0` / `iter_00` instead of `00_resample` / `mf_…_resample`.
- **Resample.** It is named by an id, not `baseline`, and recorded in `CONDITION_LABEL_MAP_iter0.json` with role `frozen_baseline`. `executor_interface_contract.md §Resample runs` has been rewritten to match (request shape, example JSON, log organisation, zero-success paragraph). The `execute_evals.py` docstring examples were updated too.
- **Private map** `investigations/<name>/CONDITION_LABEL_MAP_iter<N>.json`, with shape `{"iteration", "conditions": {id: {"name", "role"}}, "analyst_labels": {request_tag: {label: id}}}`. It extends R06's precedent: the Analyst labels still sit on top and are re-randomised per invocation. The lead writes it before launching the Executor. The Analyst's `RESTRICTED_FILES` denies it.
- **`scripts/check_blinding.py`** (mode-neutral). Run before every Analyst invocation; a non-zero exit blocks. Five-surface propagation done.
- **Docs.** Naming/label paragraphs in both lead docs, both Analyst guides, `subagent_invocation.md` Executor/Analyst examples, Designer contract (variant-naming rule, Analyst-facing leakage bullet, content-invariance mirror), and anti-patterns (Diagnostician #25, Orchestrator #17). The report Limitations/caveats lists in both modes now include blinding exposures.
- **Pre-existing logs.** Both lead docs now document R02's neutral symlink staging as a fallback only. Staging cannot fix the symlink target or channels 2–4, so the check still blocks. The lead then either re-runs the arms under ids or records the exposure (channels, conditions, measurements touched) in the log and the report's Limitations, and proceeds on that recorded decision. It never proceeds silently.

## D32 status

**Closed for new investigations.** The check passes on an opaque-id layout (test) and blocks on R02's iteration-1 layout (test, real corpus). It also blocks on R02's iteration-2 *staged* layout, which shows the documented fallback is mitigation, not a fix:

- `staged/set_E → logs/control` leaks through the symlink target.
- Every `.eval` header still carries `cond:v3`, `condition=v3` and so on.

**Residual, accepted by the owner:** `task_args`. A `-T`-driven variant is distinguishable from its header. The check warns, the docs steer towards file-edit variants, and the leak goes in the caveats.

## check_blinding.py — design

| Channel | Real names (from map) | Role words |
|---|---|---|
| label; path components (from the investigation dir — the map's dir — down when sources sit beneath it, else below their common ancestor); symlink targets of the sources and of any file/dir link beneath them; dir/file names under a source | block | block |
| header `task_file`, tags, metadata (keys and values) | block | block if the value distinguishes conditions; **warn** if identical in every condition of a multi-source request; **block** for a single source |
| header task name (incl. Inspect log-filename task segment), dataset name/location | block | as above, but warn when common even for a single source (the eval's own name) |
| rest of a whole Analyst request (topic, behaviour spec, construal checks, …) | block | not checked (a behaviour description may say "control") |
| `metadata.condition` / `cond:` tag not among the map's ids | block ("unmapped") | — |
| a source whose `.eval` headers all fail to read | block (fail closed) | — |
| `task_args` | warn: names each differing argument; names a real name inside *any* value | warn (inside a differing value) |
| task_file / directory *shape* differing once ids are masked (control on the unprefixed `task.py`; pool from another iteration) | warn | warn |

Exit codes: 0 clean, 1 block, 2 usage. All output goes to stdout. Headers are read with `header_only=True` and only naming fields are touched, so the score blackout is unaffected.

## Files changed

- New: `scripts/check_blinding.py`, `tests/test_check_blinding.py`
- `subagents/transcript_analyst/agent.py`: `RESTRICTED_FILES` entries for the map
- `tests/conftest.py`: `write_fixture_log` gains `task`, `task_file`, `tags`, `metadata`, `task_args` kwargs (defaults unchanged)
- `scripts/execute_evals.py`: two docstring examples only
- `install.sh` (link list + `GITIGNORE_ENTRIES`), `uninstall.sh`, `README.md` tree, `.claude/settings.json` allow entry
- `.claude/docs/contracts/executor_interface_contract.md`: Experiment name, Conditions, §Resample runs
- `.claude/docs/contracts/designer_interface_contract.md`: Name bullet, Side-effect content-invariance line, Information-leakage bullet, Variant naming
- `.claude/docs/investigator/diagnostician_responsibilities.md`: §1b, §1c transcript source, §3a *Assign opaque execution ids*, §3c labels bullet + check rule + pre-id fallback, Phase 5 Limitations, log-template Analyst Findings line, Anti-pattern #25
- `.claude/docs/investigator/orchestrator_responsibilities.md`: §2b naming bullet, §2c iteration name + Patterns A/B, §2d condition spec + label map, §2g transcript source + check step + fallback + What-can-go-wrong, Phase 3 caveats, log-template Conditions Constructed lines, Investigation Directory Convention tree, Anti-pattern #17
- `.claude/docs/investigator/orchestrator_analyst_guide.md`: §Providing condition labels
- `.claude/docs/investigator/diagnostician_analyst_guide.md`: §Condition labels and request assembly
- `.claude/docs/investigator/subagent_invocation.md`: Executor and Analyst examples and bullets; §Forensic-mode example path
- `.claude/docs/contracts/explorer_interface_contract.md`: §Condition naming (ids; no names or role words in diffs). Found by review.
- `.claude/skills/diagnose/perturbation-design-patterns.md`: the label-leak bullet. Found by review.

## Tests

`tests/test_check_blinding.py` has 31 tests:

- **Clean.** The opaque layout passes, both as a bare mapping and as a whole request.
- **One leaky fixture per channel.** path, task_file, tags, metadata, label, directory symlink target, file symlink target, filename, request field, and the unmapped condition name.
- **Single source.** A 1c-shaped pool under `00_resample` with `exp:…resample` blocks on both.
- **Matching.** Legacy map shape; separator, case, camelCase and inflection tolerance; `v1` does not match inside `v10`.
- **Fail closed.** Unreadable headers block.
- **Warn only, exit 0.** No `.eval` files; differing `task_args`; a real name in a shared `task_args` value; a role word common to every condition; `task_file` shape mismatch; mixed-iteration layout.
- **Usage errors** exit 2.
- **Real corpus.**
  - R02's iteration-1 layout blocks (every real name on the path; `control` on path, tags, metadata).
  - R02's iteration-2 *staged* layout blocks via the symlink target and headers, not the path.
- **Analyst read hook.** The runner's real hook denies `CONDITION_LABEL_MAP_iter{0,1,03,42}.json` and the bare name, and allows a log path.

`tests/conftest.py` defaults are unchanged.

Full suite, final run (190 before this WP):

```
$ uv run --no-project --with "inspect_ai==0.3.263" --with "inspect-scout==0.5.2" --with claude-agent-sdk --with python-dotenv --with pytest pytest tests/ -q -p no:cacheprovider
........................................................................ [ 32%]
........................................................................ [ 65%]
........................................................................ [ 97%]
.....                                                                    [100%]
221 passed in 44.47s
```

The real R02 iteration-1 request, run against R06's legacy map (role words only):

```
BLOCK  [path] condition_C: token 'control' — role word in 'control' — …/iterations/01_round1/logs/control
BLOCK  [metadata] condition_C: token 'control' — role word in 'condition=control' (distinguishes this condition) — …/logs/control/2026-09-11T22-25-21-00-00_episode_CkzRKNe83xitXJj43Zr7Wr.eval
BLOCK  [tags] condition_C: token 'control' — role word in 'cond:control' (distinguishes this condition) — …
BLOCK  [task_file] condition_C: token 'control' — role word in 'investigations/blackmail_gpt41/iterations/01_round1/conditions/control/task.py' …
check_blinding: BLOCKED — 4 blocking hit(s); do not invoke the Analyst on these sources.
```

## Review (Sonnet subagent) and what changed

The reviewer found no Critical issues, and saw no WP2-owned surface edited beyond the assigned log-template lines. All six Important findings are fixed:

1. **File-level symlink staging was a false negative.** Every symlink under a source now has its target checked.
2. **A real name in a non-differing `task_args` value was silent.** It now warns.
3. **The rest of the request was unscanned.** It is now scanned for real names, which block.
4. **The frozen pool's `task_file` was always the odd shape.** The pool now runs from an id-named copy, `c_0e4d/task.py`, per the contract and §1b.
5. **Single-source requests were vacuously "common".** Paths are now anchored at the investigation dir, and single-source role words in tags, `task_file` and metadata block.
6. **Stale contradicting text.** Fixed in `perturbation-design-patterns.md`, `explorer_interface_contract.md` §Condition naming, and the executor report's Condition column.

Minor items fixed:
- Inflections (`resampled`, `resampling`) and camelCase now match.
- Unreadable headers fail closed.
- The 3c command now carries the full map path.
- The doc now notes the expected mixed-iteration warning.

Minor items left:
- **`--allow` override.** Not added, to keep the check simple; see judgement call 2.
- **`ctrl`/`ctl`.** Not added to the role words, which stay as the owner's four.
- **Map restriction capped at iteration 99.** See judgement call 6.
- **Header-only read still loads `results` into memory.** Nothing reads or prints it.
- **Review file location.** It stays at the worktree root, as the brief asked.

## Propagation checklist (spine walk)

- [x] Lead doc (both modes): ids assigned before the Executor, map written, iteration/experiment names neutral.
- [x] Executor request: ids as condition names (contract + invocation reference examples). Runner unchanged, since `c_xxxx` passes its `[A-Za-z0-9_]+` check.
- [x] Log layout: `logs/<id>/`, `cond:<id>`, `metadata.condition=<id>`, `exp:<neutral>`.
- [x] Analyst request: labels → id directories; mapping recorded under `analyst_labels`.
- [x] `check_blinding.py` before every Analyst invocation (1c included); its warnings go to the log and the caveats.
- [x] Analyst: map denied via `RESTRICTED_FILES` (tested through the runner's real read hook). The Analyst's system prompt and request builder are unchanged, so no new input field and no `**data` hazard.
- [x] Designer: told its names never reach the model or the Analyst, and not to write names or role words into diffs or created file names.
- [x] Log template fields (both modes); report caveats (both modes); anti-patterns (both modes).
- [x] Five surfaces: `install.sh` link + `GITIGNORE_ENTRIES`, `uninstall.sh`, README tree, `settings.json` (`uv run python scripts/check_blinding.py *` — it needs `inspect_ai`, so it runs in the project env, like `dump_transcripts.py`).
- [x] Invariants: score blackout untouched (no scorer field read); pre-registration untouched; no forensic text added to Orchestrator-mode Analyst inputs; the Analyst receives neither the map nor any real name.

## Open judgement calls for the owner

1. **Path anchoring.** Path components are checked from the investigation directory down, inferred as the label map's directory. If a source is outside it, they are checked below the sources' common ancestor. Above the investigation is the user's workspace, which the lead does not name. The cost is that the check relies on the map living in the investigation dir, as both lead docs now say.
2. **Role words in non-discriminating header values warn rather than block.** This covers a value identical in every condition, e.g. a control-research eval's own task or dataset name (`control_arena…`); for a single source it applies only to task and dataset names. Without it, such an eval would block every invocation with no fix available. Real names always block, and there is no `--allow` escape hatch.
3. **Unmapped condition names block.** A `metadata.condition` / `cond:` value not among the map's ids blocks, which catches a lead that forgot to record a descriptive name. This is slightly beyond the literal spec (which blocks only on real names and role words).
4. **Symlink targets are checked.** That makes staging alone never pass. This is deliberate: the Analyst can `readlink`, and the owner's decision already says staging is a fallback that still blocks.
5. **Resample directory renamed** `00_resample` → `00_round0`, and the experiment name changed to `iter_00`. "resample" is a blocking role word, and the pool is scanned alongside later arms. Launcher `--tag 00_resample` examples (WP2-owned §General Pattern, `launch_subagent.py`) are unchanged: tags name artefacts, not transcript sources.
6. **`RESTRICTED_FILES` enumerates `CONDITION_LABEL_MAP_iter0…99`** (plain and zero-padded) plus the bare name. The runner's read hook matches non-directory patterns as suffixes, and `runner.py` is WP2's, so it couldn't be made glob-aware. A wildcard-capable matcher in `runner.py` is a cleaner follow-up. As with all `RESTRICTED_FILES`, the hook covers the Read tool only, not `cat` via Bash.
7. **Orchestrator iteration names made neutral** (`iter_01`). The old descriptive convention leaked the *hypothesis* (not the mapping) through every `exp:` tag and transcript path, contradicting the neutral-topic firewall. `check_blinding.py` cannot detect hypothesis words, so this rests on the doc rule.
8. **Real names / role words inside a `task_args` value** are folded into the non-blocking `task_args` warning, per "warns, never blocks" on that channel, even though a file-name `-T` value could be renamed.
9. **Mixed-iteration sources** (the frozen pool beside a round's arms) produce a non-blocking "differently shaped directories" warning. It discloses which source is the earlier pool. The doc says to record it, and to copy-stage the pool if a row depends on that arm staying anonymous.

## Not done, and why

- `tests/test_execute_evals.py` still uses `baseline`/`treatment` as generic condition names. The runner is name-agnostic, so the tests are not wrong, and renaming 23 references would collide with WP2's `classify_error` tests.
- **Not in scope, observed.** The Critic's `diff_review` receives Designer variant names in `diffs[].name`. A descriptive name can give away the intent its `### Inferred Purpose` is meant to test blind. This could be fixed the same way (names → ids).
- **Not in scope, observed.** The forensic addendum lets the caller supply "a purpose-built control condition" for the 1c floor check. That is a scoped, intentional role disclosure and was left as is.
- **Not in scope, observed.** The Analyst contract does not tell the Analyst to report any path or header that seems to reveal a condition's identity. Such a self-report rule would have surfaced R02's breach during the round rather than after it.
- `scripts/dump_transcripts.py` docstring examples (`control/`, `no_deadline/`) were left alone. Those dumps go to the Mapper/Critic, not the Analyst.
