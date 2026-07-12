"""Required-step state machine for the MergeWarden review workflow."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

WorkflowStepStatus = Literal[
    "pending",
    "in_progress",
    "completed",
    "skipped",
    "failed",
]
WorkflowCondition = Literal["always", "has_candidates", "has_risk_candidates"]


class ReviewWorkflowStep(BaseModel):
    step_id: str
    phase: int
    required: bool = True
    required_if: WorkflowCondition = "always"


class ReviewWorkflowStepState(BaseModel):
    step_id: str
    status: WorkflowStepStatus = "pending"
    attempts: int = 0
    skip_reason: str = ""
    failure_reason: str = ""


DEFAULT_REVIEW_WORKFLOW: tuple[ReviewWorkflowStep, ...] = (
    ReviewWorkflowStep(step_id="inspect_diff", phase=10),
    ReviewWorkflowStep(step_id="inspect_changed_context", phase=20),
    ReviewWorkflowStep(
        step_id="validate_candidate_draft",
        phase=30,
        required_if="has_candidates",
    ),
    ReviewWorkflowStep(
        step_id="semantic_verify_findings",
        phase=40,
        required_if="has_risk_candidates",
    ),
    ReviewWorkflowStep(step_id="finalize_review", phase=50),
)


class ReviewWorkflowTracker:
    """Track one review run's bounded, ordered workflow steps."""

    def __init__(
        self,
        steps: tuple[ReviewWorkflowStep, ...] = DEFAULT_REVIEW_WORKFLOW,
        *,
        max_attempts: int = 2,
    ) -> None:
        self.steps = tuple(sorted(steps, key=lambda item: item.phase))
        self.states = {
            step.step_id: ReviewWorkflowStepState(step_id=step.step_id)
            for step in self.steps
        }
        self.max_attempts = max(1, max_attempts)

    def start(self, step_id: str) -> ReviewWorkflowStepState:
        step = self._step(step_id)
        state = self.states[step_id]
        if state.status in {"completed", "skipped"}:
            raise ValueError(f"step {step_id} is terminal")
        missing = [
            prior.step_id
            for prior in self.steps
            if prior.phase < step.phase
            and prior.required
            and self.states[prior.step_id].status not in {"completed", "skipped"}
        ]
        if missing:
            raise ValueError(f"missing predecessor: {', '.join(missing)}")
        if state.attempts >= self.max_attempts:
            raise ValueError(f"step {step_id} reached attempt limit")
        state.status = "in_progress"
        state.attempts += 1
        state.failure_reason = ""
        return state

    def complete(self, step_id: str) -> ReviewWorkflowStepState:
        state = self.states[self._step(step_id).step_id]
        if state.status in {"completed", "skipped"}:
            if state.status == "completed":
                return state
            raise ValueError(f"step {step_id} is terminal")
        if state.attempts == 0:
            state.attempts = 1
        state.status = "completed"
        state.failure_reason = ""
        return state

    def skip(
        self, step_id: str, reason: str, *, condition_not_applicable: bool = False
    ) -> ReviewWorkflowStepState:
        if not reason.strip():
            raise ValueError("skip reason is required")
        if not condition_not_applicable:
            raise ValueError("required steps may only be skipped when not applicable")
        state = self.states[self._step(step_id).step_id]
        if state.status == "completed":
            raise ValueError(f"step {step_id} is terminal")
        state.status = "skipped"
        state.skip_reason = reason.strip()
        return state

    def fail(self, step_id: str, reason: str) -> ReviewWorkflowStepState:
        state = self.states[self._step(step_id).step_id]
        if state.status != "in_progress":
            raise ValueError(f"step {step_id} is not in progress")
        state.status = "failed"
        state.failure_reason = reason.strip()
        return state

    def retry(self, step_id: str) -> ReviewWorkflowStepState:
        state = self.states[self._step(step_id).step_id]
        if state.status != "failed":
            raise ValueError(f"step {step_id} is not failed")
        if state.attempts >= self.max_attempts:
            raise ValueError(f"step {step_id} reached attempt limit")
        return self.start(step_id)

    def missing_required(
        self,
        *,
        has_candidates: bool,
        has_risk_candidates: bool,
    ) -> list[ReviewWorkflowStep]:
        return [
            step
            for step in self.steps
            if self._is_required(
                step,
                has_candidates=has_candidates,
                has_risk_candidates=has_risk_candidates,
            )
            and self.states[step.step_id].status != "completed"
        ]

    def summary(
        self,
        *,
        has_candidates: bool,
        has_risk_candidates: bool,
    ) -> dict[str, object]:
        required = [
            step
            for step in self.steps
            if self._is_required(
                step,
                has_candidates=has_candidates,
                has_risk_candidates=has_risk_candidates,
            )
        ]
        completed = [
            step
            for step in required
            if self.states[step.step_id].status == "completed"
        ]
        missing = self.missing_required(
            has_candidates=has_candidates,
            has_risk_candidates=has_risk_candidates,
        )
        return {
            "required_step_count": len(required),
            "completed_required_step_count": len(completed),
            "missing_required_steps": [step.step_id for step in missing],
            "states": {
                key: value.model_dump(mode="json") for key, value in self.states.items()
            },
        }

    def _step(self, step_id: str) -> ReviewWorkflowStep:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise KeyError(step_id)

    @staticmethod
    def _is_required(
        step: ReviewWorkflowStep,
        *,
        has_candidates: bool,
        has_risk_candidates: bool,
    ) -> bool:
        if not step.required:
            return False
        if step.required_if == "has_candidates":
            return has_candidates
        if step.required_if == "has_risk_candidates":
            return has_risk_candidates
        return True
