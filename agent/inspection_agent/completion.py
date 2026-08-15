from __future__ import annotations

from agent.inspection_agent.state import InspectionContext
from agent.runtime.contracts import CompletionDecision


class InspectionCompletionGate:
    """Quantitative completion contract evaluated outside the model."""

    def evaluate(self, state: InspectionContext) -> CompletionDecision:
        missing: list[str] = []
        errors: list[str] = []
        no_change_reason = str(getattr(state, "no_change_reason", "") or "")
        if state.report is None and not no_change_reason:
            missing.append("report")
        if state.verification is None:
            missing.append("verification")
        elif not state.verification.ok:
            missing.append("verification_passed")
            errors.extend(state.verification.errors)
        if state.tool_failures:
            missing.append("failed_tools_recovered")
            errors.extend(
                f"{name}: {message}"
                for name, message in sorted(state.tool_failures.items())
            )
        return CompletionDecision(
            complete=not missing,
            reason=(
                "verified_no_change"
                if not missing and no_change_reason
                else "verified_project_output"
                if not missing
                else "project_output_incomplete"
            ),
            missing=missing,
            errors=errors,
        )
