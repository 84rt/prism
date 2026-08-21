# Session B Brief — Analyst Extension and Executor Resample (Tasks 5, 6)

You are extending Prism's Transcript Analyst so it can grade a *named behaviour* with a reusable, fixed instrument, and documenting how the Executor is invoked to resample a scenario.

**Read first, in this order:**
1. `docs/superpowers/plans/2026-08-14-model-forensics-mode.md` — Global Constraints, D4, D7, D8, D11, D13, then Tasks 5 and 6 in full.
2. `.claude/docs/analyst_interface_contract.md` — the file you are extending. Read all of it.
3. `subagents/transcript_analyst/{agent,system_prompt}.py` — the code you are extending.
4. `.claude/docs/model_forensics_principles.md §11` — blinding, manipulation checks, instrument stability. This section is the *rationale* for everything you are about to write.

---

## Your file allowlist

**Modify only these:**

```
subagents/transcript_analyst/agent.py
subagents/transcript_analyst/system_prompt.py
.claude/docs/analyst_interface_contract.md
.claude/docs/executor_interface_contract.md
```

**Explicitly forbidden**, with reasons:
- `.claude/docs/subagent_invocation.md` — you will want to document `behaviour_spec` here. Don't. Four tasks want to edit this file and Task 6b absorbs all four in one pass.
- `install.sh`, `uninstall.sh`, `README.md` — Session C and Task 9 own these.
- `subagents/environment_mapper/`, `subagents/variant_designer/`, `subagents/redteam_critic/` and their contracts — Session A is creating these right now.
- `subagents/runner.py`, `subagents/cli.py` — nothing you need requires changing them.

**Do not commit.** The repo owner commits by hand and is merging three sessions by review.

---

## The single hardest constraint on this session

**Everything you do must be purely additive.** An Orchestrator investigation run after your changes must behave identically to one run before them. The Orchestrator is exercised, working software; the Model Forensics mode does not exist yet. If you are ever choosing between a cleaner design and a strictly-additive one, take additive.

Concretely: `behaviour_spec` is an **optional** request field. When it is absent, every code path and every instruction must be what it is today. Verify this at the end with `git diff subagents/transcript_analyst/agent.py` and confirm the only `-` lines are context-free whitespace.

---

## Task 5: what `behaviour_spec` is for

Model Forensics measures *one named behaviour* across many perturbations of one scenario. That requires a **fixed instrument**: a grader built once and applied unchanged everywhere. A grader rebuilt per invocation is a different instrument each time, and a change in the measured rate then cannot be distinguished from a change in the measurement — which destroys the only quantity the whole investigation tracks.

The Analyst is deliberately stateless across invocations (no memory file, by design, to keep the hypothesis firewall structural). So grader reuse cannot be an instruction — it needs an interface. That is `grader_dir`: absent or empty means build-and-persist; present and populated means load-and-apply-unmodified.

```json
"behaviour_spec": {
    "description": "The model edited the test file rather than the implementation",
    "reference_transcript": "/abs/path/to/original.eval",
    "grader_dir": "/abs/path/to/artefacts/analyst/grader",
    "manipulation_check": "Whether the transcript shows the model treating the deadline as binding"
}
```

### The firewall moves, and you need to understand how

This is the subtlest thing in the task. In Orchestrator mode the Analyst never learns *what* it is measuring for — it gets a neutral topic only. In forensic mode it is told the behaviour. That is not a breach:

| | Orchestrator | Diagnostician |
|---|---|---|
| Analyst knows the hypothesis / candidate explanations | Never | **Never** — unchanged |
| Analyst knows which condition is which | Never | **Never** — unchanged |
| Analyst knows what behaviour to measure | No | **Yes** — new |

Telling the Analyst "grade for behaviour A" leaks nothing about *why* anyone thinks A happened. The blinding that matters — condition opacity, ignorance of the investigator's explanations — is untouched. Say this explicitly in the contract, because a reader who knows the Orchestrator's design will otherwise read the new field as a firewall breach.

### Forensic mode is two measurements, never one

The fixed grader gives comparable rates; the open-ended neutral-topic scan gives anomalies. **Both, every time.** The open-ended half is not decoration — it is a primary route to explanations nobody had thought of, because a better account of the episode usually first shows up as a behaviour the grader was never built to detect. It gets its own report section (`### Behaviours not captured by the grader`), reported even when empty, because a silent omission is indistinguishable from not having looked.

### Grader validity: three checks, not one

- **Design from the original episode, validate against the resampled population.** The behaviour class is defined by the episode under investigation, so building the grader from resamples risks measuring something else. But a grader built from one trajectory can key on that trajectory's surface form.
- **Floor check** — a negative-control set on which the grader must score zero. Prefer realistic near-misses from the resample pool; a purpose-built structurally-impossible variant may be supplied instead when the pool has too few clear negatives.
- **Borderline analysis** — inspect the cases near the decision boundary. If what separates them from clear positives is *wording* rather than the behaviour, the grader has keyed on surface form. This, not the floor check, is what catches that failure.

### The manipulation check

New second scanner, and the reason it exists is the sharpest empirical finding in the source literature: an intervention intended to clarify user intent left the model *still confused*, visible only on inspection — the edit did not move the latent it targeted. Report a verdict of `succeeded` / `failed` / `ambiguous` explicitly and do not soften a failure, because **a null under a failed manipulation is uninterpretable, not disconfirming**. Treating it as disconfirming is the easiest available way to eliminate a true explanation.

**Where it runs — decided, do not re-litigate.** A **separate scanner pass over the same transcripts**, ideally on a different scanner model. Not separate rollouts. The experimental-psychology caution about checking on the same subjects concerns *demand effects*, which need a channel to influence the subject — a transcript scan has none, since the model finished long ago. The real contamination risk is in the grader: one pass judging both "did the behaviour occur" and "did the construal shift" lets the second be coloured by the first, and a distinct blind pass removes that at zero extra eval cost. Separate rollouts would roughly double per-variant spend to buy independence you already have.

Also required in forensic mode: an **eval-awareness split** — evaluation awareness (inferring it is being tested for a property and gaming it) reported separately from simulation awareness (merely noticing artificiality). The two carry very different inferential risk and collapsing them loses that.

---

## Task 6: the finding you are inheriting

Task 0 already checked this, and the plan's original instruction was wrong — do not repeat it. `scripts/execute_evals.py` consumes **pre-built command strings** and never sees `overrides` at all; override rendering is the *Executor agent's* job in its system prompt.

What is actually true:
- `epochs` and `runs_per_condition` are both documented in `executor_interface_contract.md §Overrides`.
- `--epochs` is implemented at `subagents/experiment_executor/system_prompt.py:76`.
- **`runs_per_condition` appears in the contract but nowhere in the system prompt.** It is specified and plausibly unimplemented.

So Task 6 may be larger than "documentation only". Decide between: adding `runs_per_condition` to the Executor's system prompt, or specifying the 1b resample using `epochs` and documenting the difference. The difference is real — **epochs re-run samples within one eval; `runs_per_condition` re-runs the whole eval**, which matters whenever the eval carries within-transcript or cross-sample state. If you touch `subagents/experiment_executor/system_prompt.py`, that is a fourth file beyond your allowlist; it is not contested by another session, but flag the change clearly for review since it affects the Orchestrator's Executor too.

The resample invocation itself: exactly one condition named `baseline`, unmodified task, no `-T` overrides, its own log directory. A zero success count here is **terminal for the investigation**, not a retry situation — the behaviour could not be reproduced — so the Executor should report it plainly rather than attempting recovery.

---

## Verification

```bash
git diff --stat subagents/transcript_analyst/          # expect 2 files
git diff subagents/transcript_analyst/agent.py         # expect additive only
grep -c "behaviour_spec" subagents/transcript_analyst/agent.py   # expect 2
uv run ruff check subagents/transcript_analyst/        # expect clean
```

There is no test suite in this repo; ruff is the mechanical gate.

---

## One thing that will trip you

**The `**data` prompt-builder pattern fails silently.** `run_transcript_analyst` builds a `sections` list and joins it. If you add `behaviour_spec` to the contract but forget the corresponding `if data.get("behaviour_spec"):` block in `agent.py`, nothing errors — the Analyst simply never receives it, and you get a normal-looking report measuring the wrong thing. After editing the contract, walk each request field against the prompt builder one by one.
