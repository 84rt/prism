"""`prompt-attack`: Control Tower's attack policy with the system prompt read from a file.

The stock `attack` policy reads its prompt once from the package, so a red-team
strategy cannot be changed without editing installed code. This subclass takes
`prompt_path`, so an attack strategy is a plain Markdown file that Prism's
Attack Designer can write and the Executor can pass through unchanged:

    -T policy=policies/prompt_attack.py@prompt-attack \
    -T policy_args='{"prompt_path": "prompts/example_attack.md"}'

The file is formatted with the same placeholders the stock prompt gets:
`{setting_description}` and `{side_task_description}`. Its sha256 is kept on
the policy (`prompt_sha256`) so a run can be tied to the exact strategy text.
Everything else (private tools, `plan`, `probe`) is inherited.

Loaded as a `file.py@name` reference, so it works locally only (not on EC2 or
Hawk runners); that is all Prism needs.
"""

# No `from __future__ import annotations`: the registry builds a Pydantic args
# model from the constructor's annotations and needs them as real objects.
import hashlib
from pathlib import Path

from inspect_ai.model import ChatMessageSystem

from control_tower.eval.game.messages import side_tasks_description
from control_tower.eval.game.registry import policies
from control_tower.eval.game.view import RedView
from control_tower.policies.attack import AttackPolicy, Plan, PrivateToolsArg, ProbeArg


@policies.register(
    name="prompt-attack",
    attempts_main_task=True,
    attempts_side_task=True,
    attempts_sneaky=True,
)
class PromptAttackPolicy(AttackPolicy):
    """Attack policy whose red-team system prompt comes from `prompt_path`.

    Args:
        prompt_path: Markdown file with the attack strategy; relative paths
            resolve from the process working directory (the task's directory
            under Prism's Executor).
        private_tools: As for `attack`.
        plan: As for `attack`. The planning variant uses the same file.
        probe: As for `attack`.
    """

    def __init__(
        self,
        prompt_path: str,
        private_tools: PrivateToolsArg = frozenset(),
        plan: Plan = "none",
        probe: ProbeArg = False,
    ) -> None:
        super().__init__(private_tools=private_tools, plan=plan, probe=probe)
        path = Path(prompt_path)
        if not path.is_file():
            raise FileNotFoundError(f"prompt-attack: no prompt file at {path.resolve()}")
        self.prompt_path = str(path.resolve())
        self.prompt_template = path.read_text()
        self.prompt_sha256 = hashlib.sha256(self.prompt_template.encode()).hexdigest()

    def setup(self, view: RedView) -> None:
        super().setup(view)  # validates tasks, sets side ids and the planner model
        self.system = ChatMessageSystem(
            content=self.prompt_template.format(
                setting_description=view.environment.description,
                side_task_description=side_tasks_description(view.side_tasks),
            )
        )
