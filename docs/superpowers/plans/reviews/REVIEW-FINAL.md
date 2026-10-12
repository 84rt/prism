# Final-round fixes — review notes

Worktree `/workspaces/prism-final` (branch `final`, from `72df293`). Everything is uncommitted; no git operations were run beyond `status`/`diff`/`log`. No API spend.

## Verification

- Full suite: **593 passed** (554 before; 39 new tests), after the reviewer fixes. Command as in the brief.
- `ruff check scripts subagents tests --select F,B`: **13 findings, all pre-existing** (B905 `zip()` without `strict=` and similar in untouched code). A sorted, line-number-stripped comparison against the main checkout gives an identical set: **nothing new**.
- `check_blinding.py` re-run read-only against the real smoke-run request (`smoke-diagnose/.../requests/iter_00_analyst_1.json`): it now **blocks** `[reference] condition_B` and warns on task name, epochs (20 vs 30), created date (2026-10-12 vs 2026-09-09) and the "earlier" note.

## Per item

### 1. The original episode never becomes a condition
- `scripts/check_blinding.py`: a new **block** channel, `reference`: any transcript-source `.eval` whose realpath equals the request's `behaviour_spec.reference_transcript`, or whose bytes match it (sizes are compared first, so only a same-size log gets hashed). It **warns** when conditions differ in header `task`, `config.epochs` or `created` date (`profile_warnings`), and when `execution_notes` contain role words or `earlier/original/reference/previous/prior/flagged` (`notes_warnings`; `prior` does not match `priority`). A reference path that can't be read gives a warning.
- Docs: `diagnostician_responsibilities.md` §1c (fresh pool only; the reference is never a condition, with the smoke-run precedent), Phase 2 ("Dump without scores, pick without scores"; to stratify, use the Analyst's 1c per-sample verdicts and draw at random within each stratum; `--with-scores` is never for the lead on a transcript source), §3b blackout paragraph (also covers the reference's other epochs). `diagnostician_analyst_guide.md` (1c build). `subagent_invocation.md` §Transcript handoff (score-free dumps; reference never a condition). `dump_transcripts.py` docstring. The addendum tells the Analyst to report, not measure, a source that looks identical to the reference.
- Tests (`tests/test_check_blinding.py`): a synthetic copy of the smoke-run leak (copied reference in `00_round0/logs/c_9bb5`) blocks and raises all three header warnings plus the provenance warning; the reference's own directory used as a source blocks; a clean forensic request carrying a reference stays clean (0 warnings); provenance words are parametrised; an epoch-only difference warns. `conftest.write_fixture_log` gained a `created=` parameter.

### 2. Permissions for unattended runs
- New `scripts/check_keys.py`: reports ANTHROPIC/OPENROUTER/OPENAI (plus any `--key NAME`) as `present`/`empty`/`missing`, from the environment and the nearest `.env` at or above the cwd (`--env-file` overrides). It never prints a value, always exits 0, and has `--json`. Wired on all five surfaces. Tests in `tests/test_lead_helpers.py` check that a planted secret never appears in either output format.
- `.claude/settings.json`: the two `ask` rules are now `deny`, and there is no `ask` key at all. Test: `test_settings_ship_no_ask_rules`.
- The `.env` bypass is now denied: `Bash(* .env)`, `Bash(* .env *)`, `Bash(*/.env)`, `Bash(*/.env *)`, `.env.local` variants, quoted `'.env'`/`".env"`, `*python*load_dotenv*` and `*python*dotenv_values*` (which match through any interpreter path, so they close the smoke-run `.venv/bin/python` + dotenv route). Blanket `.venv/bin/python*` denies were added and then removed at the owner's review (2026-10-12): leads legitimately use the venv's interpreter to verify environment copies (the smoke-run lead regenerated each arm's prompts with it), and the dotenv patterns already close the `.env` route; a test pins both. `Bash(*dotenv*)` was rejected as too broad, because the sub-agent launch line carries `--with python-dotenv`; a test pins that the launch line stays allowed.
- What can't be enforced (documented in the README): indirection (`F=.env; cat $F`, `.e*` globs), a script that opens the file itself, and `printenv`/`echo $VAR` of keys already exported. `printenv` stays allow-listed (unchanged); those cases rest on the doc rule plus `check_keys.py`.
- Docs: Orchestrator Phase 1 step 5 and its auth-error signature; the Diagnostician cross-family gate; both SKILLs (a new "Working rules for an unattended run" block); both lead docs' State Management ("Files and the shell"). All say: never read or source `.env`, and use `check_keys.py`.
- Hashing: the RESUME template's grader-hash command is now `find … | sort | xargs sha256sum` (diagnose SKILL resume step updated, with a fallback note for an older `RESUME.md`). Added `Bash(xargs sha256sum)` / `Bash(xargs sha256sum *)` to the allow list, in line with the existing read-only `xargs` entries.
- README: a new "Running unattended" subsection.

### 3. Shell friction
- Both lead docs (State Management) and both SKILLs: write and append with Write/Edit, never shell redirection; no shell loops (one command per item, or a script).
- New `scripts/new_ids.py`: N fresh `c_<4 hex>` ids from `secrets`, skipping every id found in `--map` files or directories (any `CONDITION_LABEL_MAP*.json`). It exits 2 on an unreadable map or an impossible N. Wired on five surfaces; tests cover the shape, collision avoidance (forced draws) and usage errors. The opaque-id instructions point at it in Orchestrator Step 2b and Diagnostician 1b and 3a.

### 4. Analyst firewall gaps
- Request drafts now go under `investigations/<inv>/requests/drafts/`. This is stated in `subagent_invocation.md` §General Pattern, both lead docs, both SKILLs, and the Critic contract's `ledger_to_json.py -o` example. Confirmed: directory patterns are matched with `fnmatch` over `*/investigations/*/requests/*`, so subdirectories are covered, and a test proves it through the real hook. The launcher's `list` scans receipts, not `requests/`, so drafts don't show up as invocations.
- New Analyst `RESTRICTED_FILES`: `investigations/*/request_drafts/` (legacy), `investigations/*/transcripts/` (dump dirs under any iteration layout) and `artefacts/reference/`. The Analyst doesn't need any of them: in the smoke-run artefacts it reads logs through Scout and writes its own dumps as JSON under `artefacts/analyst/`. The denial reasons are worded mode-neutrally (no "behaviour_spec", no "Critic"), so the Orchestrator-mode Analyst meets no forensic text even when a denial fires.
- Tests: a parametrised block/allow table through Read and Bash hooks, and a Grep rooted above a dump dir that is denied while a Grep over `logs/` is allowed. One existing test used `iter_01/transcripts/a.txt` as its example "allowed" path; that path is now deliberately denied for the Analyst, so it was changed to a log path.

### 5. Critics get score-free transcripts
- Updated `critic_interface_contract.md` (`transcript_paths`, and `evidence_summary` may describe the instrument at any iteration), `subagent_invocation.md` (handoff and Critic field notes), and `diagnostician_responsibilities.md` (Phase 2 invocation and 3d step 8). The Critic builder already renders `evidence_summary` at any iteration, so no code change was needed.

### 6. No auto-backgrounding inside sub-agents
- `subagents/runner.py`: every sub-agent's `ClaudeAgentOptions.env` now carries `BASH_DEFAULT_TIMEOUT_MS=600000` (10 min, above the Executor's 540 s `wait`), `BASH_MAX_TIMEOUT_MS=3600000` (60 min, for long scans) and `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`. A timeout value the user exported wins.
- The **names are verified** in the bundled CLI of the pinned SDK (claude-agent-sdk 0.2.165, bundled CLI 2.1.294) by string search. That `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` also stops timeout-triggered auto-backgrounding is based on Claude Code's documentation, not something I observed directly. It is belt-and-braces: the 10-minute default alone covers the smoke-run case.
- Tests: the env reaches the options on both routes, and a user override wins.

### 7. Low items
- **Preamble strip:** `subagents/cli.strip_preamble` drops everything before the first line starting with `### `, applied where every report is printed. A report with no `###` line is left untouched for `check_report.py` to flag, and `check_report.py`'s warning is unchanged. A test also checks that `check_report` sees no preamble afterwards. The lead doc's "read structurally" note is updated.
- **`openrouter_endpoints.py --list <prefix>`:** lists live catalogue ids by prefix (case-insensitive, `openrouter/` dropped), newest first, with $/M in, $/M out, context length and created date. It makes one public request. Tests use a canned catalogue with the network mocked. The cross-family gate docs now resolve the Critic through `--list` per family and then `<id> --prefer cost`. The stale written shortlist is gone, and the Critic-failure fallback points at `--list` too.
- **K-ids:** `normalise_construal_checks(spec, id_prefix="M")`; forensic mode passes `"K"`, Orchestrator mode is unchanged. The forensic docs (analyst guide, responsibilities 3c, addendum examples, `subagent_invocation.md` forensic section) use `K<n>`. The Orchestrator-mode docs keep `M<n>`. Test: forensic mode mints `K1`, Orchestrator mode mints `M1`.

### Deferred (notes only)
- Score-blackout hook: both lead docs' blackout paragraphs say the blackout is behavioural beyond the `private/` deny, that the final `.eval`s live outside `private/`, and that a time-gated hook is future work.
- `spend.py` pricing and scanner capture: noted in the `spend.py` docstring and both budget sections (keep headroom; the figure under-counts).
- Generic environment-copy helper: not implemented, and no doc note added (none was asked for at a specific place). Recorded here only.

## Judgement calls
- `check_blinding.py` can only find the reference when it is given the **whole request**; a bare mapping has no reference. The docs already invoke it with the whole request.
- The header-profile `created` warning compares UTC dates and fires only on a gap of more than one day (changed after review), so a round straddling midnight stays clean.
- `notes_warnings` flags role words in `execution_notes` as warnings, even though role words in the rest of the request aren't checked. The notes are meant to be purely non-directional.
- `printenv` stays allowed: removing it was outside the agreed scope and could break unrelated habits. The exposure is documented instead.
- `strip_preamble` lives in `subagents/cli.py` rather than `runner.py`, because the CLI is where every report is emitted and it covers both direct and launcher runs.
- New deny rules match any command that takes `.env` as a bare argument (for example `test -f .env`). The old docs told leads to run `test -f .env` and `grep -c '^KEY=' .env`; those instructions are replaced by `check_keys.py`.

## Reviewer pass
One reviewer subagent (Sonnet) reviewed the diff against the scope and invariants. It found **no Critical issues**. It checked these invariants in code, settings and tests: score blackout, label map, isolation, no `ask` rules, five-surface wiring, docstrings and the K/M prefix split. **Gap:** the auto-mode classifier denied it `git diff` on `.claude/docs` and `.claude/skills`, so it reviewed those only by grepping for `.env`, `dotenv`, `.venv/bin/python` and `find -exec`. It found no doc instructing a command the new deny rules block. I then grepped the docs myself for stale instructions (`test -f .env`, `grep -c` key checks, `--with-scores` permissions, `-exec sha256sum`, `request_drafts`, scratchpad drafts); none remain. **The docs diff has not had an independent read; worth a skim before committing.**

Fixed from the review:
- Important — the `request_drafts/` denial reason named "Critic and Designer". It is reworded to be neutral, and a new test asserts that no Analyst denial reason contains a `FORENSIC_ONLY_TERMS` entry. Pre-existing reasons already name the Designer, Critic and Mapper roles; that is unchanged and not forensic-only text.
- Important — the `created` warning would fire on a round that straddles midnight UTC. It now warns only when dates are more than one day apart (`_date_span_days`, with a test).
- Important — unset `epochs` now normalises to `1`, so `None` and `--epochs 1` no longer warn falsely.
- Important — `*load_dotenv*` / `*dotenv_values*` were narrowed to `*python*load_dotenv*` / `*python*dotenv_values*`, so a code search for `load_dotenv` is no longer denied while interpreter one-liners still are. `check_keys.py`'s docstring now says `--env-file` takes a differently named file, because a bare `.env` argument is denied.
- Minor — `reference_copies` tolerates dangling links and unreadable logs. A `reference_transcript` that is not an `.eval` now warns (test). The `new_ids.py` id regex finds ids embedded after `_` (test). `--list` errors go to stderr as plain text. The preamble that `strip_preamble` removes is logged to the invocation's stderr instead of being silently dropped. The `runner.py` comment notes that a user-exported timeout below 540 s reintroduces the problem.

Not changed (deliberate):
- Provenance words such as `reference`/`prior` in ordinary notes can warn. This is acceptable, since it is a warning.
- `check_keys.py` misreads `KEY=v#comment` without a space and multi-line values. It can only misreport presence, never leak a value.
- `--list` ignores `--prefer`.
