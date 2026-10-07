# REVIEW-WP4 — Critic interface & candidate handoff

Worktree `/workspaces/prism-wp4`, branch `wp4`. Nothing committed or staged.

## D-IDs

| ID | Status | What closes it |
|---|---|---|
| D38 | closed | `scripts/ledger_to_json.py`: parses the two live sections of `CANDIDATES.md` per §State Management, emits the contract's `{id, slug, statement, discriminating_signature, status}` array, exits 1 with a defect list and writes nothing when a live candidate lacks *Statement* or *Discriminating signature*. Case-5's C10 (the `iter2_request.json` L104–109 exhibit) is refused by name; case-1's output is byte-identical to R01's exemplar serialiser. |
| D33 / D09 | closed | `scripts/check_report.py <role> <report.md> [--mode]`: verbatim level-3 headings, order, duplicates, substitutions named, fenced code ignored; for `critic --mode candidate_critique`, every proposed candidate must carry a signature (six proposal formats seen in the corpus are recognised, plus fenced JSON). Contract now states the checklist-in-request and the one-re-ask rule; the request carries the mode's headings verbatim. |
| D10 | closed | `agent.py` resolves the contract path from the scaffold root via `realpath(__file__)` (follows the install symlink back to the checkout), injects it as `## Contract` in every mode, and raises `RuntimeError` (exit 2) if the file is absent. Wave 3 generalisation to all six agents noted in a TODO. |
| D11 | closed | `## Before Proposing` reminder rendered after the ledger in `candidate_critique`; system prompt and contract state merge-by-signature on receipt (caller-side dedup wording; the mechanical half of merging is WP5-A's). |

## Files changed

- new `scripts/ledger_to_json.py`, `scripts/check_report.py`
- `subagents/redteam_critic/agent.py` (contract path, heading checklist, D11 reminder, optional `conformance_defects` re-ask field), `subagents/redteam_critic/system_prompt.py`
- `.claude/docs/contracts/critic_interface_contract.md` (cross-family section untouched)
- five surfaces for both scripts: `install.sh` `link_file` + `GITIGNORE_ENTRIES`, `uninstall.sh`, README tree, `.claude/settings.json` allow entries
- new `tests/` (`conftest.py`, `test_ledger_to_json.py`, `test_check_report.py`, `test_critic_prompt.py`)

Not touched: `runner.py`, Executor, Analyst, `diagnostician_responsibilities.md`, `subagent_invocation.md`, the cross-family-routing sentence.

## Tests

`uv run --with "inspect_ai==0.3.263" --with "inspect-scout==0.5.2" --with claude-agent-sdk --with python-dotenv --with pytest pytest tests/ -q` → **68 passed** (corpus-backed tests skip cleanly if `/workspaces/forensics-cases` is absent).

Corpus sweep (no API spend): serialiser over all six ledgers — case-1/2/4/6 serialise, case-5 refuses on C10, case-3 refuses on ten genuinely prose entries (real ledger defects, not parser misses). `check_report.py` over every saved Critic report: all R03/R05/R06 candidate critiques and the R05/R06 predictions pass; R04's Gemini reports, R02's merged-section Gemini reports, R06's bold-pseudo-heading iter-3 diff review and R03's reworded prediction fail with the substitution named. All six Mapper and ten Designer reports pass.

Invariants: no change to the Analyst, Executor, runner, or execution scripts, so the score blackout and the immutable pre-registration table are untouched. `test_diff_review_prompt_still_withholds_candidates` guards the Critic blinding.

## Propagation checklist

- [x] contract → `agent.py` prompt builder → system prompt → report consumer (check_report) walked for the new field and the checklist
- [x] `install.sh` link_file list  [x] `GITIGNORE_ENTRIES`  [x] `uninstall.sh`  [x] README tree  [x] `settings.json`
- [ ] **`subagent_invocation.md §Red-Teaming Critic`** — needs one line documenting the optional `conformance_defects` field and the `check_report.py` step. That file is a coordination hotspot outside WP4's ownership, so it is flagged here rather than edited (the memory's standing invariant — every `data.get` key documented there — is violated until it lands; Wave 3 or owner).

## Open judgement calls

1. **Re-ask mechanism = new optional field.** `conformance_defects` lets the re-ask name the defects instead of blindly re-running. Costs the `subagent_invocation.md` line above. Alternative (no new field): re-run the identical request and rely on the checklist alone.
2. **Trailer overrides section in the serialiser.** An entry under a live section whose heading says `MERGED`/`RETIRED`/`ELIMINATED`/`WITHDRAWN` is skipped with a warning (case-5 has three). The contracts say merged candidates are absent; the alternative is to emit them and trust the section.
3. **Preamble is a warning, not a defect** in `check_report.py` — the Diagnostician already reads structurally, and a re-ask for a preamble alone wastes an invocation. Non-`X` ids on proposals are likewise a warning.
4. **Executor and Analyst not in `check_report.py`.** Their contracts are being rewritten by WP1/WP3 and carry parameterised sub-headings; a table shipped now would go stale mid-wave. Extension point documented; Wave 3 item.
5. **Signature-less proposal after a failed re-ask.** The contract allows the Diagnostician to author the signature with recorded authorship (R02's practice) or drop the proposal. Whether authoring should be forbidden outright is the owner's call.
6. Case-3's ledger has ten prose live entries and two doubled `### … · ### …` heading lines; the serialiser refuses it. That is the intended behaviour, but it means a real ledger from a completed run would need tidying before the tool accepts it.

## Reviewer findings

Independent reviewer subagent (read-only, ran the suite: 68 passed). Blocking issues: none. Verdict: D38, D33/D09, D10, D11 all closed. Non-blocking: `FIELD_LINE`'s permissive separator is inert because only two exact labels are forwarded (worth a comment); the candidate-block heuristics in `check_report.py` are necessarily fuzzy for freeform model output, so a novel format could pass silently — mitigated by the one-re-ask/log-and-fall-back design. Confirmed symlink-robust `realpath` resolution, the five surfaces, `conformance_defects` read by the builder, diff_review blinding intact, and no out-of-ownership touches.
