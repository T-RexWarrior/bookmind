"""Remediation service — orchestrates the misconception closure lifecycle.

ARCHITECTURE §2 dependency rule: this is the service-layer use case that wires
the Diagnostician's task generation, the shared Task Validator, and the
Learning Engine's state transitions together, all under project scope.

The flow (LEARNING_MODEL §9):

    CONFIRMED --start_remediation--> REMEDIATING
    REMEDIATING --first changed-task indep PASS--> VERIFYING
    VERIFYING --second distinct-scenario changed-task indep PASS--> RESOLVED
    RESOLVED --new high-disc probe support--> RELAPSED

This service generates and validates the two changed tasks of different
scenarios and renders the remediation content (explanation / positive example /
counterexample). It does NOT submit answers — those go through the normal
``submit_answer`` path so Evidence and state transitions are written by the
single transactional entry point.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..agents.bug_library import BUG_LIBRARY, BugEntry
from ..domain.enums import Level
from ..domain.models import RemediationPlan, TrustedTaskContext, ValidationReport
from ..engine.learning_engine import start_remediation
from ..engine.task.generator import generate_changed_task
from ..engine.task.validator import validate
from ..llm.router import ModelRouter
from ..storage.protocols import Repository, ScopeError


@dataclass
class ChangedTaskResult:
    stage: int
    report: ValidationReport
    trusted: TrustedTaskContext | None


class RemediationService:
    """The misconception remediation use case, scoped to a project."""

    def __init__(self, repo: Repository, router: ModelRouter | None = None) -> None:
        self.repo = repo
        self.router = router

    # --- content ---------------------------------------------------------

    def render_plan(self, bug_id: str) -> RemediationPlan:
        """Render the remediation content for a bug from its BugEntry."""
        bug = BUG_LIBRARY.get(bug_id)
        if bug is None:
            raise KeyError(f"unknown bug {bug_id}")
        return RemediationPlan(
            bug_id=bug.bug_id,
            explanation_goal=bug.remediation.explanation_goal,
            positive_example=bug.remediation.positive_example,
            counterexample=bug.remediation.counterexample,
            rubric=list(bug.rubric),
            changed_task_stages=[1, 2],
        )

    # --- lifecycle -------------------------------------------------------

    def start(self, *, project_id: str, bug_id: str) -> RemediationPlan:
        """Flip CONFIRMED → REMEDIATING and return the rendered plan.

        Raises if the bug is unknown or no misconception record exists.
        """
        if bug_id not in BUG_LIBRARY:
            raise KeyError(f"unknown bug {bug_id}")
        mis = start_remediation(self.repo, project_id=project_id, bug_id=bug_id)
        if mis is None:
            raise ScopeError(f"no misconception record for {bug_id} in project {project_id}")
        return self.render_plan(bug_id)

    def build_changed_task(
        self,
        *,
        project_id: str,
        bug_id: str,
        stage: int,
        target_concept_ids: list[str] | None = None,
        level: Level = Level.L3,
    ) -> ChangedTaskResult:
        """Generate + validate one changed task for a remediation stage.

        Returns a :class:`ChangedTaskResult` whose ``trusted`` is None if the
        validator rejected the draft (``report`` explains why).
        """
        if stage not in (1, 2):
            raise ValueError("stage must be 1 (near) or 2 (far)")
        bug = BUG_LIBRARY.get(bug_id)
        if bug is None:
            raise KeyError(f"unknown bug {bug_id}")
        targets = target_concept_ids or list(bug.related_concepts)
        draft = generate_changed_task(
            bug, stage=stage, target_concept_ids=targets, level=level, router=self.router,
        )
        report = validate(draft, self.repo, project_id)
        return ChangedTaskResult(stage=stage, report=report, trusted=report.trusted)
