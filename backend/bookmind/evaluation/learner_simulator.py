"""L3 deterministic learner simulator — EVALUATION.md §4.

The simulator is the closed-loop benchmark's engine. It is **fully
deterministic**: given the same profile, seed, interaction budget and model
budget it reproduces the same behaviour, so adaptive systems can be compared
without "forcing shared question sequences" (EVALUATION §4.2).

A ``SimulatedLearner`` carries:
  - ``mastery_gt`` — ground-truth mastery per concept (L0..L4);
  - ``misconception_gt`` — which bugs are ACTIVE vs NONE;
  - ``response_policy`` — how hints, slips and the ground truth map to a
    PASS/PARTIAL/FAIL answer at a given level;
  - ``fixed_seed`` — seeds a local ``random.Random`` so every decision is
    reproducible.

The simulator does NOT prove real-student teaching effect (EVALUATION §9). It
validates state updates, diagnosis timing, Next Action, intervention counts,
recovery flow and reproducibility.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable

from ..domain.enums import EvidenceResult, Level


@dataclass
class LearnerProfile:
    """One deterministic learner profile (EVALUATION §4: 10–20 profiles)."""

    profile_id: str
    description: str
    mastery_gt: dict[str, Level]  # concept_id → true level
    misconception_gt: dict[str, str]  # concept_id → bug_id that is ACTIVE ("none" if clean)
    slip_rate: float = 0.0  # P(an independent PASS answer slips to PARTIAL)
    hint_dependence: float = 0.0  # P(the learner needs a hint on a hard task)
    seed: int = 42


@dataclass
class SimulatedResponse:
    """The learner's response to one task."""

    result: EvidenceResult
    used_hint: bool
    answer_text: str
    # Misconception signal the learner's answer would produce, if any.
    bug_id: str | None = None
    signal_direction: str = "FOR"  # FOR (shows the bug) / AGAINST (doesn't)
    signal_strength: str = "MEDIUM"


class SimulatedLearner:
    """A deterministic simulated learner driven by a fixed seed."""

    def __init__(self, profile: LearnerProfile, *, learning_enabled: bool = True) -> None:
        self.profile = profile
        self._rng = random.Random(profile.seed)
        # Per-concept practice count: repeated attempts let the learner
        # improve by one effective level every ``practice_to_learn`` tries
        # (a simple, deterministic learning model so the adaptive loop is not
        # stuck quizzing a concept the learner never passes).
        self._attempts: dict[str, int] = {}
        self._practice_to_learn = 2
        self._learning_enabled = learning_enabled

    def _effective_gt(self, concept_id: str) -> Level:
        """Ground truth bumped by practice: every ``_practice_to_learn`` attempts
        on a concept the learner has not yet mastered raises their effective
        level by one (capped at L4). This models learning-by-doing so the
        closed loop can make progress without an external tutor."""
        gt = self.profile.mastery_gt.get(concept_id, Level.L0)
        attempts = self._attempts.get(concept_id, 0)
        bumps = attempts // self._practice_to_learn
        eff_rank = min(_level_rank(gt) + bumps, _level_rank(Level.L4))
        return _rank_to_level(eff_rank)

    # --- response generation ---------------------------------------------

    def respond(
        self,
        *,
        concept_id: str,
        required_level: Level,
        independent: bool,
        is_probe: bool = False,
        discriminated_bug_id: str | None = None,
    ) -> SimulatedResponse:
        """Produce a deterministic response to a task at ``required_level``.

        Rules (all reproducible):
          - If the learner's ground-truth mastery >= required_level and no
            active misconception bites, they PASS (with a small slip chance →
            PARTIAL).
          - If ground-truth < required_level, they FAIL (or PARTIAL if close).
          - If the task is a probe for an ACTIVE bug, the learner produces the
            bug's predicted wrong answer (FOR signal, STRONG).
          - Hint dependence: on HARD concepts (gt < level), the learner may need
            a hint; if independent is required and no hint is allowed, they FAIL.
        """
        gt = self.profile.mastery_gt.get(concept_id, Level.L0)
        active_bug = self.profile.misconception_gt.get(concept_id)
        if active_bug is None or active_bug == "none":
            active_bug = None

        # Probe: the learner reveals their active bug (or answers correctly).
        # Probes do not advance practice (they diagnose, not teach).
        if is_probe and active_bug is not None and discriminated_bug_id == active_bug:
            return SimulatedResponse(
                result=EvidenceResult.FAIL,
                used_hint=False,
                answer_text=f"(predicted wrong answer for {active_bug})",
                bug_id=active_bug,
                signal_direction="FOR",
                signal_strength="STRONG",
            )
        if is_probe and active_bug is not None and discriminated_bug_id is not None and discriminated_bug_id != active_bug:
            # The probe targets a different bug — learner answers for their bug,
            # which discriminates AGAINST the probed hypothesis.
            return SimulatedResponse(
                result=EvidenceResult.FAIL,
                used_hint=False,
                answer_text=f"(answer consistent with {active_bug}, not {discriminated_bug_id})",
                bug_id=discriminated_bug_id,
                signal_direction="AGAINST",
                signal_strength="MEDIUM",
            )

        # Mastery-based answer. Non-probe practice attempts advance the
        # learning model (deterministic: every 2 attempts bumps effective gt).
        # B0/B1 baselines disable this — they represent "last answer = mastery"
        # with no learning-by-doing.
        if not is_probe and self._learning_enabled:
            self._attempts[concept_id] = self._attempts.get(concept_id, 0) + 1
        eff_gt = self._effective_gt(concept_id) if self._learning_enabled else gt
        gt_rank = _level_rank(eff_gt)
        req_rank = _level_rank(required_level)

        if gt_rank >= req_rank:
            # Knows it — small slip chance → PARTIAL.
            if self._rng.random() < self.profile.slip_rate:
                return SimulatedResponse(
                    result=EvidenceResult.PARTIAL, used_hint=False,
                    answer_text="(slipped — mostly right with a gap)",
                )
            return SimulatedResponse(
                result=EvidenceResult.PASS, used_hint=False,
                answer_text="(correct, independent)",
            )

        # Doesn't know it at this level.
        # Hint dependence: may need a hint on a hard task.
        used_hint = False
        if gt_rank < req_rank - 1 and self._rng.random() < self.profile.hint_dependence:
            used_hint = True
            # With a hint, a close learner might get PARTIAL; far learner still fails.
            if gt_rank >= req_rank - 1:
                return SimulatedResponse(
                    result=EvidenceResult.PARTIAL, used_hint=True,
                    answer_text="(hinted, partial)",
                )
            return SimulatedResponse(
                result=EvidenceResult.FAIL, used_hint=True,
                answer_text="(hinted but still wrong)",
            )

        # No hint, doesn't know: PARTIAL if close (within 1 level), else FAIL.
        if gt_rank >= req_rank - 1:
            return SimulatedResponse(
                result=EvidenceResult.PARTIAL, used_hint=False,
                answer_text="(close — partial credit)",
            )
        # An active bug on this concept biases a fail toward the bug's wrong answer.
        if active_bug is not None:
            return SimulatedResponse(
                result=EvidenceResult.FAIL, used_hint=False,
                answer_text=f"(wrong — shows {active_bug})",
                bug_id=active_bug, signal_direction="FOR", signal_strength="MEDIUM",
            )
        return SimulatedResponse(
            result=EvidenceResult.FAIL, used_hint=False,
            answer_text="(wrong — doesn't know yet)",
        )

    # --- profile facts ---------------------------------------------------

    def knows(self, concept_id: str, at_least: Level) -> bool:
        return _level_rank(self.profile.mastery_gt.get(concept_id, Level.L0)) >= _level_rank(at_least)

    def has_bug(self, concept_id: str) -> str | None:
        b = self.profile.misconception_gt.get(concept_id)
        return b if b and b != "none" else None


def _level_rank(level: Level) -> int:
    return {Level.L0: 0, Level.L1: 1, Level.L2: 2, Level.L3: 3, Level.L4: 4}[level]


def _rank_to_level(rank: int) -> Level:
    order = [Level.L0, Level.L1, Level.L2, Level.L3, Level.L4]
    return order[max(0, min(rank, len(order) - 1))]


# --- a small fixed profile bank (EVALUATION §4: 10–20 profiles) -------------

def default_profiles() -> list[LearnerProfile]:
    """A small deterministic profile bank covering the §4 axes:
    0–3 misconceptions, multi-chapter, a slip, hint dependence, long absence."""
    profiles: list[LearnerProfile] = []

    # P1: a strong learner, no misconceptions, mostly L2+ across the core.
    profiles.append(LearnerProfile(
        profile_id="P1_strong",
        description="Strong learner: L2+ on most core concepts, no bugs, low slip",
        mastery_gt={
            "c_variable": Level.L2, "c_reference": Level.L2, "c_object": Level.L2,
            "c_method": Level.L2, "c_string": Level.L2, "c_class": Level.L2,
            "c_inheritance": Level.L2, "c_polymorphism": Level.L2,
            "c_interface": Level.L1, "c_collection_hierarchy": Level.L1,
            "c_value_equality": Level.L2, "c_hashcode": Level.L1,
        },
        misconception_gt={},
        slip_rate=0.05, hint_dependence=0.0, seed=101,
    ))

    # P2: a learner with the reference-vs-object bug, mid mastery.
    profiles.append(LearnerProfile(
        profile_id="P2_ref_bug",
        description="Mid learner with reference-vs-object confusion",
        mastery_gt={
            "c_variable": Level.L2, "c_reference": Level.L1, "c_object": Level.L1,
            "c_method": Level.L2, "c_string": Level.L1, "c_class": Level.L1,
            "c_inheritance": Level.L1, "c_polymorphism": Level.L0,
        },
        misconception_gt={"c_reference": "bug_ref_vs_object"},
        slip_rate=0.1, hint_dependence=0.1, seed=102,
    ))

    # P3: a learner with the equals-without-hashCode bug.
    profiles.append(LearnerProfile(
        profile_id="P3_eq_hash",
        description="Learner who forgets hashCode when overriding equals",
        mastery_gt={
            "c_variable": Level.L2, "c_reference": Level.L2, "c_string": Level.L2,
            "c_reference_equality": Level.L2, "c_value_equality": Level.L1,
            "c_equals_contract": Level.L1, "c_hashcode": Level.L0,
            "c_class": Level.L2, "c_inheritance": Level.L1,
        },
        misconception_gt={"c_hashcode": "bug_equals_no_hashcode"},
        slip_rate=0.08, hint_dependence=0.15, seed=103,
    ))

    # P4: a beginner, mostly L0/L1, no bugs yet.
    profiles.append(LearnerProfile(
        profile_id="P4_beginner",
        description="Beginner: L0/L1 across the board, no misconceptions yet",
        mastery_gt={
            "c_variable": Level.L1, "c_reference": Level.L0, "c_object": Level.L0,
            "c_method": Level.L1, "c_control_flow": Level.L1,
            "c_string": Level.L0, "c_class": Level.L0,
        },
        misconception_gt={},
        slip_rate=0.15, hint_dependence=0.25, seed=104,
    ))

    # P5: a learner with the == vs equals bug and higher slip.
    profiles.append(LearnerProfile(
        profile_id="P5_eq_vs_equals",
        description="Learner using == for content comparison, high slip",
        mastery_gt={
            "c_variable": Level.L2, "c_reference": Level.L2, "c_string": Level.L1,
            "c_reference_equality": Level.L1, "c_value_equality": Level.L0,
            "c_class": Level.L2, "c_inheritance": Level.L1, "c_polymorphism": Level.L1,
        },
        misconception_gt={"c_reference_equality": "bug_eq_vs_equals"},
        slip_rate=0.2, hint_dependence=0.2, seed=105,
    ))

    return profiles
