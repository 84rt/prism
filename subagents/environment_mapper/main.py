"""CLI entry point for the Environment Mapper agent."""

import os
import sys

# Bootstrap: ensure project root is on sys.path before package imports.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from subagents.cli import AgentCLIConfig, run_cli  # noqa: E402
from subagents.environment_mapper.agent import run_environment_mapper  # noqa: E402

if __name__ == "__main__":
    run_cli(
        AgentCLIConfig(
            name="Environment Mapper",
            required_fields=[
                "environment_path",
                "behaviour_description",
            ],
            directory_field="environment_path",
            run_fn=run_environment_mapper,
        )
    )
