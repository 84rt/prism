# Session A Brief — The Three New Sub-Agents (Tasks 2, 3, 4)

You are building the Environment Mapper, the Variant Designer, and the Red-Teaming Critic for Prism's Model Forensics mode.

**Read first, in this order:**
1. `docs/superpowers/plans/2026-08-14-model-forensics-mode.md` — Global Constraints, Design decisions D1–D15, then Tasks 2, 3 and 4 in full.
2. `.claude/docs/investigator/model_forensics_principles.md` — at minimum §1 (the source agenda), §2 (diagnosticity), §3 (candidate generation), §6 (interactions), §11 (blinding).
3. `subagents/environment_explorer/` — all four files. This is your template.
4. `.claude/docs/contracts/explorer_interface_contract.md` — this is your contract template.

---

## Your file allowlist

**Create only these.** Anything outside this list belongs to another session running concurrently.

```
subagents/environment_mapper/{__init__,system_prompt,agent,main}.py
subagents/variant_designer/{__init__,system_prompt,agent,main}.py
subagents/redteam_critic/{__init__,system_prompt,agent,main}.py
.claude/docs/contracts/mapper_interface_contract.md
.claude/docs/contracts/designer_interface_contract.md
.claude/docs/contracts/critic_interface_contract.md
```

**Explicitly forbidden**, with reasons:
- `.claude/docs/investigator/subagent_invocation.md` — four tasks want to edit this; Task 6b absorbs all four in one pass. Editing it here causes three-way merge damage in the file that tells the Diagnostician how to invoke everything.
- `install.sh`, `uninstall.sh`, `README.md` — Task 9 wires everything at once; Session C may be touching them now.
- `subagents/transcript_analyst/`, `.claude/docs/{analyst,executor}_interface_contract.md` — Session B owns these.
- `subagents/runner.py`, `subagents/cli.py` — shared infrastructure, and nothing you need requires changing them.

**Do not commit.** The repo owner commits by hand and is merging three sessions by review.

---

## Why one session builds all three

They are template siblings. Each is four Python files mirroring `environment_explorer/`, and the second and third are much cheaper once the first establishes the pattern. Build the Mapper first and completely, then reuse its shape. Consistency across the three matters more than speed on any one, because Task 7 consumes all three contracts and Task 10 debugs the mismatches.

---

## The shape every sub-agent shares

Learn it from `environment_explorer/`, but the invariants are:

- `__init__.py` is empty.
- `system_prompt.py` exports a module-level `SYSTEM_PROMPT: str`. It governs *process*; the contract governs *output structure*. Every system prompt says "read your contract before beginning" and names the file.
- `agent.py` defines `ALLOWED_TOOLS`, `DISALLOWED_TOOLS`, `RESTRICTED_FILES`, and one `async def run_<name>(cwd: str | None = None, **data: Any) -> str` that builds a prompt and calls `run_agent(...)` from `subagents/runner.py`.
- `main.py` bootstraps `sys.path`, then `run_cli(AgentCLIConfig(...))`.
- All three pass `model=data.get("subagent_model")`, `thinking=data.get("subagent_thinking")`, `route=data.get("subagent_route")` through to `run_agent`. These are infrastructure fields on every sub-agent; never treat them as experimental variables.
- `memory_file=None` for all three. Sub-agent invocations are self-contained.

**NumPy-style docstrings with explicit `Parameters` / `Returns` on every function.** Match the existing files' comment density — they are heavily commented, deliberately, and inline comments explain *why* rather than *what*.

---

## Contract conventions (all three)

Copy these from `explorer_interface_contract.md` rather than reinventing:

- Section headings are **verbatim markdown headings** at the level the contract specifies, in the order given. The caller locates sections structurally, so a perfect report with unmarked headings is a report it cannot read. State this explicitly in each contract's Conventions section.
- Each invocation is self-contained; read-only and stdout-only where applicable.
- **Blocker** flag semantics are local in scope: a per-variant Blocker kills that variant, not the round.
- Asymmetric error preference: over-flag confounds rather than miss them.
- A **Non-negotiable requirements** section at the end listing the two or three commitments whose failure propagates silently downstream.

---

## What is genuinely new in each, beyond the template

**Mapper.** The Explorer receives a hypothesis; the Mapper does not — it runs *before* any candidate explanations exist. Its distinctive output is the `### Manipulable Surface Inventory`: everything that *could* be varied, hypothesis-free, with kind and coupling. Include surfaces you expect to be uninteresting, because an omitted surface is invisible to the Designer downstream. Its `### Instruction Analysis` must state the prohibition status of the target behaviour explicitly (prohibited / permitted / unmentioned / ambiguous) and quote the operative clause — the Diagnostician compares this against what the CoT shows the model *registering*, and the divergence is high-value evidence (`model_forensics_principles.md §4`).

**Designer.** Its job is *discrimination*, not coverage: not "where can I test H?" but "what edit would separate C1 from C2?" The `### Discrimination Matrix` is what makes it more than a diff generator — every candidate gets a cell for every variant, `n/a` included, and a variant whose row is identical across all live candidates should be reported as not worth running rather than padded to budget. Three obligations from the verified sources: state all three counterfactual confounds per variant (D15), propose combination variants where masking is plausible (D10), and supply positive-control variants when the Diagnostician intends to eliminate on absence of evidence (D13).

**Critic.** One agent, three modes (`candidate_critique`, `diff_review`, `prediction`), dispatched in `_build_prompt`. Mode-conditional required fields cannot be expressed in `AgentCLIConfig.required_fields`, so validate them in `_build_prompt` and raise `RuntimeError` — `run_cli` catches that and exits 2. Read `subagents/cli.py:109-114` to confirm before relying on it. The Critic is **cross-family by default** and `/diagnose` hard-blocks without `OPENROUTER_API_KEY`; note that an `OPENAI_API_KEY` does not substitute, because `runner.py` implements only two routes (`direct` → Anthropic, `openrouter`). Its ordering discipline in `candidate_critique` — read transcripts and write `### Independent Reading` *before* reading the candidate list, never revised after — is linear sequential unmasking (`model_forensics_principles.md §11`) and is the whole reason the mode exists (§9's bad-lot problem).

---

## Blinding: who may read what

`RESTRICTED_FILES` maps filename patterns to denial reasons, matched by **basename suffix**. Get this right; it is a scientific-integrity mechanism, not hygiene.

| Agent | Must not read | Why |
|---|---|---|
| Mapper | `model_forensics_principles.md`, `eval_science_principles.md`, `orchestrator_analyst_guide.md`, `analyst_interface_contract.md`, `candidate-methodology.md` | A descriptive agent that has absorbed the investigator's framework starts producing explanations instead of descriptions |
| Designer | same list | Needs the candidates themselves, but not the framework for adjudicating between them |
| Critic | `model_forensics_principles.md`, `eval_science_principles.md`, `candidate-methodology.md`, `orchestrator_analyst_guide.md` | Its value is reasoning decorrelated from the Diagnostician's; shared methodology is shared priors |

Each agent **must** be able to read its own contract — leave those out of the list deliberately, as `environment_explorer/agent.py` does with a comment saying so.

Known maintenance risk, flagged rather than solved: blocking the Critic from the principles doc also blocks it from the admissibility bar it is held to (every proposed candidate needs a discriminating signature), so that requirement is restated in the Critic's own system prompt and could drift. Do not try to fix this by relaxing the blinding.

---

## Verification you can run without API calls

Each agent's CLI validation is testable offline — `run_cli` checks required fields and directory existence before any agent runs:

```bash
mkdir -p /tmp/prism-mf-check
printf '{"environment_path": "/nonexistent"}' > /tmp/prism-mf-check/bad.json
uv run --with claude-agent-sdk --with python-dotenv python subagents/environment_mapper/main.py /tmp/prism-mf-check/bad.json; echo "exit=$?"
```
Expect a missing-required-fields error on stderr and `exit=1`. Then a second case with fields present but a bad directory → `exit=1` with the directory error. For the Critic, a bad `mode` and a mode-missing-field case should each give `exit=2`.

Lint gate (there is no test suite in this repo): `uv run ruff check subagents/`

---

## Two things that will trip you

1. **`run_agent` injects `Write` into `allowed_tools` whenever `memory_file` is set.** All three of your agents pass `memory_file=None`, which is what keeps the Mapper, Designer and Critic genuinely read-only. Do not add a memory file to any of them.
2. **The `**data` prompt-builder pattern fails silently.** If you add a field to a contract and forget to render it in `agent.py`'s prompt builder, nothing errors — the sub-agent simply never sees it. After writing each contract, walk its request fields against the prompt builder one by one.
