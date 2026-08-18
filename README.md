<img src="docs/prism-icon.png" alt="Prism icon" width="206" align="left">

# Prism: Automating Science-of-Evals Research for AI Safety

**Prism** is a scaffold for automating science-of-evaluations research in [Inspect AI](https://inspect.aisi.org.uk/) evaluations, built on [Claude Code](https://claude.com/product/claude-code), [Inspect Scout](https://meridianlabs-ai.github.io/inspect_scout/) and the [Claude Agent SDK](https://docs.anthropic.com/en/docs/agents/claude-agent-sdk).

The scaffold has two primary use cases:

- **Understanding evaluation dynamics** in frontier evals, e.g.:
  - *"How does feature X in this evaluation setting impact model behaviours?"*
  - *"Please identify any confounds that mean this eval isn't measuring what it claims to measure."*
- **Stress-testing explanations** for observed model behaviours, e.g.:
  - *"Is our current interpretation of model behaviour Y the simplest, most robust explanation?"*
  - *"Which features of the eval are causally relevant for eliciting this particular behaviour?"*

<p align="center">
  <img src="docs/scaffold-diagram.png" alt="Scaffold architecture: User talks to an Orchestrator, which delegates to an Environment Explorer, Experiment Executor, and Transcript Analyst (behind a hypothesis firewall)" width="500">
</p>

The Orchestrator agent (launched with the `/orchestrate` skill) begins by discussing the research question with the User to define its scope. It then runs the following loop, recording each iteration in an append-only investigation log:

1. **Generate falsifiable hypotheses** that are relevant and targeted to the research scope defined in the discussion phase;
2. Ask the *Environment Explorer* sub-agent to **identify informative, minimal perturbations** in the target eval setting;
3. Use the *Experiment Executor* sub-agent to **run the eval variations**, handling parallelism, error recovery, and logging;
4. Give the *Transcript Analyst* sub-agent a neutral topic description from which it can **surface relevant behavioural patterns** in the eval outputs, importantly without it ever directly seeing the original hypothesis; and
5. **Collate and interpret results** from the Analyst, then determine whether to iterate, conclude, or check in with the User.

When finished, the Orchestrator produces a structured report for the User that summarises the key findings, caveats, and potential next steps.

> **Recommended models.** We observed diminished compliance and less effective use of the scaffold's resources when running the Orchestrator on older models (e.g. Opus 4.5). We therefore recommend running the Orchestrator on the most recent Claude models for the best results.

## Prerequisites

- **[Claude Code](https://claude.com/product/claude-code)** — the Orchestrator skill and its sub-agents run inside Claude Code; the scaffold cannot be used without it.
- **API keys** — an `ANTHROPIC_API_KEY` (always required, since the Orchestrator and sub-agents run on Claude by default), plus whichever keys your target models need: an `OPENAI_API_KEY` if your eval or its grader uses OpenAI models, and/or an `OPENROUTER_API_KEY` to reach models from any provider through [OpenRouter](https://openrouter.ai/) with a single key.

## Quick Start

```bash
# Clone the scaffold and your target eval repo side by side
git clone <this-repo> prism
git clone <your-inspect-ai-eval-repo>
cd <your-inspect-ai-eval-repo>

# Install Prism into the target repo. install.sh detects host-vs-container
# and wires symlinks, .gitignore, and (host only) devcontainer.json accordingly.
/path/to/prism/install.sh .

# Add your API keys to a `.env` file (loaded automatically by the sub-agents).
# ANTHROPIC_API_KEY is always required. Add OPENAI_API_KEY if your target eval
# or its grader uses OpenAI models, and/or OPENROUTER_API_KEY to reach models
# from any provider through OpenRouter with one key.
echo "ANTHROPIC_API_KEY=sk-..." >> .env
echo "OPENAI_API_KEY=sk-..." >> .env      # optional
echo "OPENROUTER_API_KEY=sk-or-..." >> .env  # optional

# Launch Claude Code in the target repo and start an investigation:
# /orchestrate [your research question]

# To uninstall (remove symlinks and revert target repo changes):
# /path/to/prism/uninstall.sh .
```

## Using models from any provider

Adding `OPENROUTER_API_KEY` to your `.env` is the main setup step needed to evaluate models from any provider — Meta, Google, DeepSeek, Qwen, Mistral and the rest — alongside or instead of Anthropic and OpenAI models. Name them with an `openrouter/` prefix when you agree the model set with the Orchestrator, e.g. `openrouter/meta-llama/llama-3.3-70b-instruct`.

One dependency caveat: Inspect's OpenRouter provider is built on the OpenAI-compatible client and needs a recent `openai` package (it will tell you the minimum version if yours is too old). Having Inspect installed does not guarantee this, so if an OpenRouter run refuses to start, pin it on the invocation — `uv run --with "openai>=2.45.0" inspect eval ...`.

The same key also lets any *sub-agent* run on a non-Claude model, via an optional `subagent_route` field. Sub-agent roles are not restricted to Claude; the defaults are Claude only because that is what the scaffold has been exercised against.

**Why the Orchestrator pins a provider.** OpenRouter resolves one model id to one of several upstream providers, and for the same model id those providers differ in quantization, in which sampling parameters they honour, and in maximum context and completion length. Left to route freely, which provider served a run varies over time — so upstream provider becomes confounded with your experimental condition, invisibly, because the eval log records what was *requested* rather than what answered.

So the Orchestrator resolves a route for each model once, during the scoping discussion, and reuses it for the whole investigation:

1. **Direct** to the model's own provider when you have that key — fewest layers.
2. **OpenRouter, pinned** to a specific upstream provider otherwise. It enumerates the live endpoints and picks the model author's own endpoint where available, otherwise the one serving at the model's native precision (never an upcast — more expensive and no more faithful).
3. **OpenRouter, unpinned** only as a last resort, with routing recorded per sample and a caveat carried into the final report.

It announces its choice rather than blocking on you, so autonomous runs are unaffected. If you would rather optimise for cost than fidelity, or want a specific provider, say so during the scoping discussion and that preference is honoured — pinning to the cheapest endpoint removes the confound just as completely; it only trades away some fidelity to the reference model, which is then noted in the report.

You can inspect the choice yourself for any model:

```bash
uv run --no-project python scripts/openrouter_endpoints.py meta-llama/llama-3.3-70b-instruct
# add --prefer cost to rank by price instead
```

> **How the scaffold is reached.** `install.sh` picks one of two modes deterministically:
>
> - **Host install** (you ran the script outside any container): symlinks point at the in-container path `/opt/prism`. The target's `devcontainer.json` is patched to bind-mount the scaffold there; if the target has no devcontainer, one is scaffolded from `templates/devcontainer.json`. Open the target in your IDE and accept the "Reopen in Container" prompt.
> - **In-container install** (you ran the script from inside the container Claude Code will use): symlinks point at the scaffold's own path directly, no devcontainer changes are made. Launch Claude Code from inside the target repo (`cd` first, since skill discovery is rooted at the cwd).
>
> Mode is detected automatically via `/.dockerenv`, `/run/.containerenv`, and the `REMOTE_CONTAINERS` / `CODESPACES` env vars. To force a mode (e.g. on container runtimes that don't drop these markers), set `PRISM_INSTALL_MODE=in_container|host` before running install.

## Directory Structure

```
prism/
├── subagents/
│   ├── runner.py                  # Shared SDK runner + hooks
│   ├── cli.py                     # Shared CLI boilerplate
│   ├── environment_explorer/      # Eval environment analysis
│   ├── experiment_executor/       # Runs Inspect AI evals
│   └── transcript_analyst/        # Blinded transcript analysis
├── .claude/
│   ├── docs/                      # Orchestrator reference documents
│   ├── skills/orchestrate/       # Orchestrator skill definition
│   └── settings.json              # Claude Code permissions
├── scripts/
│   ├── execute_evals.py           # Parallel eval execution engine
│   └── openrouter_endpoints.py    # Provider enumeration + pin selection
├── docs/
│   └── scaffold-diagram.png       # Architecture diagram
├── templates/
│   └── devcontainer.json          # Devcontainer scaffolded into bare targets
├── .githooks/                     # Git hooks for scaffold repo
├── install.sh                     # Install into target repo
├── uninstall.sh                   # Remove from target repo
└── README.md
```
