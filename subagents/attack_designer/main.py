"""CLI entry point for the Attack Designer agent."""

import os
import sys

# Bootstrap: ensure project root is on sys.path before package imports.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from subagents.attack_designer.agent import run_attack_designer  # noqa: E402
from subagents.cli import AgentCLIConfig, run_cli  # noqa: E402

if __name__ == "__main__":
    run_cli(
        AgentCLIConfig(
            name="Attack Designer",
            required_fields=[
                "environment_description",
                "side_task_description",
                "current_prompts",
                "baseline_outcomes",
                "variants_requested",
                "output_dir",
            ],
            # `output_dir` is intentionally *not* a `directory_field`: the
            # Designer is allowed an output dir that does not exist yet and
            # creates it on first write, whereas the CLI's directory check
            # rejects a non-existent path. The weaker "exists or is creatable"
            # check is done in `run_attack_designer`, which fails as an agent
            # error (exit 2) rather than an input error (exit 1).
            run_fn=run_attack_designer,
        )
    )
