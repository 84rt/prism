"""CLI entry point for the Red-Teaming Critic agent."""

import os
import sys

# Bootstrap: ensure project root is on sys.path before package imports.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from subagents.cli import AgentCLIConfig, run_cli  # noqa: E402
from subagents.redteam_critic.agent import run_redteam_critic  # noqa: E402

if __name__ == "__main__":
    run_cli(
        AgentCLIConfig(
            name="Red-Teaming Critic",
            # `mode` is the only field required by every invocation. The
            # per-mode requirements are checked in the agent's _build_prompt,
            # which raises RuntimeError and so exits 2 rather than 1.
            required_fields=["mode"],
            # No directory_field: the Critic's path fields differ by mode
            # (`environment_path` only in diff_review), and the CLI validates
            # one fixed field for every invocation.
            run_fn=run_redteam_critic,
        )
    )
