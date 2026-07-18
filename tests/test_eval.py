from __future__ import annotations

import pytest

from mewcode.eval import (
    DeterministicEvalRunner,
    EvalCase,
    EvalObservation,
)


@pytest.mark.asyncio
async def test_deterministic_eval_enforces_tool_and_token_contracts() -> None:
    cases = [
        EvalCase("read", "inspect a file", expected_tools=("ReadFile",), max_tokens=20),
        EvalCase("safe", "answer directly", forbidden_tools=("Bash",)),
    ]

    async def execute(case: EvalCase) -> EvalObservation:
        if case.case_id == "read":
            return EvalObservation(True, tokens=12, tools_used=("ReadFile",))
        return EvalObservation(True, tokens=8, tools_used=())

    report = await DeterministicEvalRunner().run(cases, execute)

    assert report.passed
    assert report.success_rate == 1.0
    assert report.average_tokens == 10.0
    assert report.to_dict()["failures"] == []


@pytest.mark.asyncio
async def test_deterministic_eval_reports_reproducible_failures() -> None:
    cases = [EvalCase("write", "modify file", expected_tools=("WriteFile",))]

    report = await DeterministicEvalRunner().run(
        cases,
        lambda _case: EvalObservation(
            success=True,
            tokens=30,
            tools_used=("ReadFile",),
        ),
    )

    assert not report.passed
    assert report.failures == (
        {"case_id": "write", "reason": "missing expected tools: WriteFile"},
    )
