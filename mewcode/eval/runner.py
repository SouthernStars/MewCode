"""Replay-free deterministic evaluation for CI regression gates."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    prompt: str
    expected_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class EvalObservation:
    success: bool
    tokens: int = 0
    tools_used: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True, slots=True)
class EvalReport:
    case_count: int
    success_count: int
    success_rate: float
    average_tokens: float
    failures: tuple[dict[str, str], ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.case_count > 0 and self.success_count == self.case_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_count": self.case_count,
            "success_count": self.success_count,
            "success_rate": self.success_rate,
            "average_tokens": self.average_tokens,
            "failures": list(self.failures),
        }


Executor = Callable[[EvalCase], EvalObservation | Awaitable[EvalObservation]]


class DeterministicEvalRunner:
    """Run fixed cases sequentially and apply explicit acceptance rules."""

    async def run(
        self,
        cases: Sequence[EvalCase],
        executor: Executor,
    ) -> EvalReport:
        observations: list[tuple[EvalCase, EvalObservation]] = []
        for case in cases:
            result = executor(case)
            observation = await result if inspect.isawaitable(result) else result
            observations.append((case, observation))

        failures: list[dict[str, str]] = []
        successful = 0
        total_tokens = 0
        for case, observation in observations:
            total_tokens += max(observation.tokens, 0)
            reason = self._failure_reason(case, observation)
            if reason:
                failures.append({"case_id": case.case_id, "reason": reason})
            else:
                successful += 1

        count = len(observations)
        return EvalReport(
            case_count=count,
            success_count=successful,
            success_rate=successful / count if count else 0.0,
            average_tokens=total_tokens / count if count else 0.0,
            failures=tuple(failures),
        )

    @staticmethod
    def _failure_reason(case: EvalCase, observation: EvalObservation) -> str:
        if not observation.success:
            return observation.error or "executor reported failure"
        used = set(observation.tools_used)
        missing = [tool for tool in case.expected_tools if tool not in used]
        if missing:
            return f"missing expected tools: {', '.join(missing)}"
        forbidden = sorted(used.intersection(case.forbidden_tools))
        if forbidden:
            return f"forbidden tools used: {', '.join(forbidden)}"
        if case.max_tokens is not None and observation.tokens > case.max_tokens:
            return f"token budget exceeded: {observation.tokens}>{case.max_tokens}"
        return ""
