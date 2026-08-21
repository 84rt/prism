"""CLI entry point for the Variant Designer agent."""

import os
import sys

# Bootstrap: ensure project root is on sys.path before package imports.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from subagents.cli import AgentCLIConfig, run_cli  # noqa: E402
from subagents.variant_designer.agent import run_variant_designer  # noqa: E402

if __name__ == "__main__":
    run_cli(
        AgentCLIConfig(
            name="Variant Designer",
            required_fields=[
                "mapper_report_path",
                "environment_path",
                "candidates",
                "regime",
                "environment_rate",
                "variant_budget",
            ],
            # The environment path is the working copy the diffs apply
            # against; the Mapper report path is a file, not a directory, so
            # the CLI's directory check cannot cover it.
            directory_field="environment_path",
            run_fn=run_variant_designer,
        )
    )
