from abc import ABC, abstractmethod
from collections import defaultdict
from typing import TYPE_CHECKING, Iterable, Mapping, NamedTuple

from .models import (
    Assignment,
    Operation,
    Question,
    QuestionScore,
    RubricItem,
    RubricItemApplied,
    Student,
    Submission,
)

if TYPE_CHECKING:
    from . import CourseManager


class GradeContext(NamedTuple):
    """Imported data available to a grade operation."""

    student: Student
    assignment: Assignment
    submission: Submission
    questions: tuple[Question, ...]
    question_scores: tuple[QuestionScore, ...]
    rubric_items: tuple[RubricItem, ...]
    rubric_items_applied: tuple[RubricItemApplied, ...]


class GradeState(NamedTuple):
    """The mutable-by-replacement state passed through grade operations."""

    score: float | None
    comments: tuple[str, ...] = ()
    waived_lateness_hours: float = 0.0


def lateness_hours(lateness: str) -> float:
    """Convert a Gradescope H:M:S lateness value to hours."""
    hours, minutes, seconds = (int(part) for part in lateness.split(':'))
    return hours + minutes / 60 + seconds / 3600


class Policy(ABC):
    """Generate and resolve one ordered type of grade operation."""

    type: str

    def __init__(
        self,
        course: "CourseManager",
        id: str,
        priority: int,
        assignment_ids: Iterable[str] | None = None,
        assignment_types: Iterable[str] | None = None,
        student_ids: Iterable[str] | None = None,
    ):
        """
        Configure a policy instance and its optional target scope.

        :param course:            course manager that owns this policy
        :param id:                unique identifier for this policy instance
        :param priority:          operation priority; lower values run first
        :param assignment_ids:    optional assignment IDs to target
        :param assignment_types:  optional assignment types to target
        :param student_ids:       optional student IDs to target
        """
        self.course = course
        self.id = id
        self.priority = priority
        self.assignment_ids = frozenset(assignment_ids or ())
        self.assignment_types = frozenset(assignment_types or ())
        self.student_ids = frozenset(student_ids or ())

    def submissions(self) -> list[Submission]:
        """Return imported submissions within this policy's target scope."""
        assignments = {
            assignment.id: assignment
            for assignment in self.course.get_rows(Assignment)
        }
        return [
            submission
            for submission in self.course.get_rows(Submission)
            if (
                (not self.assignment_ids or submission.assignment_id in self.assignment_ids)
                and (
                    not self.assignment_types
                    or assignments[submission.assignment_id].type in self.assignment_types
                )
                and (not self.student_ids or submission.sid in self.student_ids)
            )
        ]

    @abstractmethod
    def generate_operations(self) -> Iterable[Operation]:
        """Generate operations from the current imported database state."""

    @abstractmethod
    def apply(
        self,
        context: GradeContext,
        grade: GradeState,
        parameters: Mapping[str, object],
    ) -> GradeState:
        """Apply this policy's operation to a grade state."""


class LatePenaltyPolicy(Policy):
    """Apply a point deduction for unwaived late submission time."""

    type = "late_penalty"

    def __init__(self, *args, points_per_day: float, **kwargs):
        """
        Configure a late-penalty policy.

        :param points_per_day: points deducted for each unwaived late day
        """
        super().__init__(*args, **kwargs)
        self.points_per_day = points_per_day

    def generate_operations(self) -> Iterable[Operation]:
        for submission in self.submissions():
            if lateness_hours(submission.lateness) > 0:
                yield Operation(
                    submission.sid,
                    submission.assignment_id,
                    {'points_per_day': self.points_per_day},
                )

    def apply(self, context, grade, parameters):
        if grade.score is None:
            return grade
        unwaived_hours = max(
            lateness_hours(context.submission.lateness) - grade.waived_lateness_hours,
            0,
        )
        deduction = float(parameters['points_per_day']) * unwaived_hours / 24
        if deduction == 0:
            return grade
        return grade._replace(
            score=grade.score - deduction,
            comments=grade.comments + (f"Late penalty: -{deduction:g} points",),
        )


class SlipDaysPolicy(Policy):
    """Allocate a student's finite slip-day allowance across late submissions."""

    type = "slip_days"

    def __init__(self, *args, allowance_days: float, **kwargs):
        """
        Configure automatic slip-day allocation.

        :param allowance_days: number of late days available to each student
        """
        super().__init__(*args, **kwargs)
        self.allowance_days = allowance_days

    def generate_operations(self) -> Iterable[Operation]:
        submissions_by_student = defaultdict(list)
        for submission in self.submissions():
            submissions_by_student[submission.sid].append(submission)

        for submissions in submissions_by_student.values():
            remaining_days = self.allowance_days
            for submission in sorted(
                submissions,
                key=lambda item: (item.submission_time, item.assignment_id),
            ):
                days_used = min(remaining_days, lateness_hours(submission.lateness) / 24)
                if days_used <= 0:
                    continue
                remaining_days -= days_used
                yield Operation(
                    submission.sid,
                    submission.assignment_id,
                    {'days_used': days_used},
                )

    def apply(self, context, grade, parameters):
        days_used = float(parameters['days_used'])
        return grade._replace(
            waived_lateness_hours=grade.waived_lateness_hours + 24 * days_used,
            comments=grade.comments + (f"Slip days used: {days_used:g}",),
        )


class ExtensionPolicy(Policy):
    """Grant additional lateness forgiveness to a policy's target scope."""

    type = "extension"

    def __init__(self, *args, extension_days: float, **kwargs):
        """
        Configure an extension policy.

        :param extension_days: number of late days forgiven for each target
        """
        super().__init__(*args, **kwargs)
        self.extension_days = extension_days

    def generate_operations(self) -> Iterable[Operation]:
        for submission in self.submissions():
            yield Operation(
                submission.sid,
                submission.assignment_id,
                {'days': self.extension_days},
            )

    def apply(self, context, grade, parameters):
        days = float(parameters['days'])
        return grade._replace(
            waived_lateness_hours=grade.waived_lateness_hours + 24 * days,
            comments=grade.comments + (f"Extension: {days:g} days",),
        )


class AdjustmentPolicy(Policy):
    """Add a fixed point adjustment to a policy's target scope."""

    type = "score_adjustment"

    def __init__(self, *args, points: float, reason: str = "", **kwargs):
        """
        Configure a fixed score adjustment.

        :param points: points added to each target grade
        :param reason: optional explanation included in final grade comments
        """
        super().__init__(*args, **kwargs)
        self.points = points
        self.reason = reason

    def generate_operations(self) -> Iterable[Operation]:
        for submission in self.submissions():
            yield Operation(
                submission.sid,
                submission.assignment_id,
                {'points': self.points, 'reason': self.reason},
            )

    def apply(self, context, grade, parameters):
        if grade.score is None:
            return grade
        points = float(parameters['points'])
        reason = str(parameters.get('reason', ''))
        comment = f"Score adjustment: {points:+g} points"
        if reason:
            comment += f" ({reason})"
        return grade._replace(
            score=grade.score + points,
            comments=grade.comments + (comment,),
        )
