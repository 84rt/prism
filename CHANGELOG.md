# Changelog

Each push to this repository is a release. Versions follow [semantic versioning](https://semver.org/) below 1.0: the minor number goes up when the lead agents' instructions, the sub-agent contracts or the eval runner change in ways that could change an investigation's findings; the patch number covers fixes that should not. Each investigation records the exact Prism commit it ran on (see *Reproducibility* below), so a finding can always be traced to the instructions that produced it.

## [0.2.0] - 2026-11-13

A cost and run-time pass over both pipelines, aiming to keep their rigour unchanged.

### Changes that can affect findings

- Sub-agents run on Claude Opus 5.5 with reasoning effort pinned at high.
- The Transcript Analyst scans with Claude Haiku 5.5 by default, moving a scanner to Sonnet 5.5 only if Haiku fails that scanner's validation. Cross-family validation is still required; its default validator is now GPT-6.1 Terra (GPT-6.1 Sol for a scanner Terra struggles with, Gemini 4 Hydrogen when the target is OpenAI's), which agreed closely with the previous GPT-5.5 labels at about a quarter of the cost.
- In model forensics, one Analyst now covers rounds of up to about twelve conditions; larger rounds are split across Analysts as before.
- The Orchestrator's instructions now consistently cap autonomous work at three iterations before it checks in with the user, unless the agreed scope says otherwise.
- The Orchestrator surveys each candidate eval and model for headroom before designing experiments (`scripts/headroom.py`), and designs no iteration on a cell already at floor or ceiling.
- Both lead agents size each round (episodes, hours and dollars) before launching it.

### Eval runner

- Automatic concurrency: unless a request sets `max_parallel` or `max_connections`, the runner measures the sandboxes its start-up checks open and runs as many samples at once as memory and cores allow (two per core, at most 40). It splits the machine evenly with any other Prism runs and re-checks its share before each condition starts; runs set by hand keep their level and the automatic ones split what is left. Both lead agents now leave concurrency to this unless the user has agreed a constraint.
- Start-up checks run side by side, give up after fifteen minutes with a plain "provider not responding" reason, and keep each failed check's own log error.
- Conditions start as slots free rather than in fixed groups.
- A clean stop command; runs remove the sandbox containers and images their killed evals leave behind.
- The runner refuses to start when Inspect cannot be imported, instead of failing every check.
- Finished samples are counted while each eval runs.

### Reproducibility

- Each investigation records its Prism commit, any local edits, its Python package versions and the model names providers actually served.
- `install.sh <repo> --pin` freezes a copy of Prism inside the eval repo between runs, so editing or pulling Prism cannot change a running investigation. Local edits can be kept as `.prism-local.patch` and are reapplied to each new copy.
- `uninstall.sh` and `install.sh --pin` refuse while an investigation is running there.

### Spend and monitoring

- `scripts/spend.py` totals the lead agent, the sub-agents, the evals and the Analyst's scanner calls, and can price offline from a cached price list.
- `scripts/run_status.py` gives a read-only summary of a running investigation: phase, round, active helper, timings and spend.
- The lead says what it is waiting for before each quiet wait.

### Cost

- The Executor and Analyst use a one-hour prompt cache, and the Executor waits longer between checks on a running eval.
- The lead waits for sub-agents in the background instead of polling, and loads reference documents only when it needs them.
- The lead agents' instructions were trimmed of repeats and old anecdotes.

## [0.1.0] - 2026-10-22

First public release: the Orchestrator (`/orchestrate`) for science-of-evals investigations and the Diagnostician (`/diagnose`) for model forensics, with their sub-agents, the eval runner, and the install and uninstall scripts.

[0.2.0]: https://github.com/LAThomson/prism/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/LAThomson/prism/releases/tag/v0.1.0
