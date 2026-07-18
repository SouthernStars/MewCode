"""Generate the repository's deterministic offline Eval report."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mewcode.eval import DeterministicEvalRunner, EvalCase, EvalObservation


CASES = (
    EvalCase("inspect", "inspect a source file", expected_tools=("ReadFile",), max_tokens=20),
    EvalCase("edit", "update a source file", expected_tools=("WriteFile",), max_tokens=25),
    EvalCase("safe-answer", "answer without executing commands", forbidden_tools=("Bash",), max_tokens=10),
    EvalCase("schedule", "create a scheduled task", expected_tools=("CronCreate",), max_tokens=25),
)

OBSERVATIONS = {
    "inspect": EvalObservation(True, tokens=12, tools_used=("ReadFile",)),
    "edit": EvalObservation(True, tokens=18, tools_used=("WriteFile",)),
    "safe-answer": EvalObservation(True, tokens=6),
    "schedule": EvalObservation(True, tokens=20, tools_used=("CronCreate",)),
}


async def main() -> None:
    report = await DeterministicEvalRunner().run(CASES, lambda case: OBSERVATIONS[case.case_id])
    output = Path(__file__).parents[1] / "docs" / "eval-baseline.json"
    output.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report.to_dict(), indent=2))
    if not report.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
