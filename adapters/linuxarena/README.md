# LinuxArena adapter (Control Tower)

Files that let Prism's Executor run LinuxArena evals through Control Tower
without the `ct` CLI. They are meant to be copied into (or symlinked from) a
directory that holds a Python 3.13 venv with `control-tower==6.0.1` and the
`ct settings pull linuxarena` environments; see `task.py`'s docstring for the
environment variables the caller must set.

- `task.py` — Prism-runnable `@task` wrapper around `control_tower/eval`;
  binds model roles explicitly (Inspect's `--model` does not set the
  `untrusted` role).
- `policies/prompt_attack.py` — `prompt-attack`: Control Tower's attack
  policy with the red-team system prompt read from a file (`prompt_path`),
  so an attack strategy is a Markdown file the Attack Designer writes.
- `prompts/example_attack.md` — the stock prompt, as the starting strategy.
- `requests/` — Executor request JSONs for the two free dry runs
  (`policy=test`, and `prompt-attack` on `mockllm/model`).

Pinned: control-tower 6.0.1, ct-linuxarena v0.4.0, inspect-ai 0.3.282.
