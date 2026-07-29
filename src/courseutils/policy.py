from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, NamedTuple

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
    waived_lateness: timedelta = timedelta()


def parse_timedelta(value: str) -> timedelta:
    """Parse Gradescope and Python ``timedelta`` string representations."""
    if ', ' in value:
        day_part, value = value.split(', ', maxsplit=1)
        days = int(day_part.removesuffix(' days').removesuffix(' day'))
    else:
        days = 0
    hours, minutes, seconds = value.split(':')
    return timedelta(
        days=days,
        hours=int(hours),
        minutes=int(minutes),
        seconds=float(seconds),
    )


@dataclass(frozen=True)
class Adjustment:
    """A fixed-point or current-score percent adjustment to a grade value."""

    points: float | None = None
    percent: float | None = None

    def __post_init__(self):
        """Require exactly one adjustment representation."""
        if (self.points is None) == (self.percent is None):
            raise ValueError("specify exactly one of points or percent")

    def __add__(self, grade: float | int) -> float:
        """Apply this adjustment when it appears left of a numeric grade.

        A percent value is expressed in percentage points, so ``-10``
        subtracts ten percent of the current grade.
        """
        if not isinstance(grade, (float, int)):
            return NotImplemented
        if self.points is not None:
            return grade + self.points
        return grade * (1 + self.percent / 100)

    def __radd__(self, grade: float | int) -> float:
        """Apply this adjustment when it appears right of a numeric grade."""
        return self + grade

    def to_dict(self) -> dict[str, float]:
        """Return a JSON-serializable representation of this adjustment."""
        if self.points is not None:
            return {'points': self.points}
        return {'percent': self.percent}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "Adjustment":
        """Create an adjustment from its serialized operation parameters."""
        points = value.get('points')
        percent = value.get('percent')
        return cls(
            points=float(points) if points is not None else None,
            percent=float(percent) if percent is not None else None,
        )


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

    def __init__(
        self,
        course: "CourseManager",
        id: str,
        priority: int,
        penalty: Callable[[float, timedelta], float],
        assignment_ids: Iterable[str] | None = None,
        assignment_types: Iterable[str] | None = None,
        student_ids: Iterable[str] | None = None,
    ):
        """
        Configure a late-penalty policy.

        :param penalty:           function returning a score for given lateness
        :param assignment_ids:    optional assignment IDs to target
        :param assignment_types:  optional assignment types to target
        :param student_ids:       optional student IDs to target
        """
        super().__init__(
            course,
            id,
            priority,
            assignment_ids,
            assignment_types,
            student_ids,
        )
        self.penalty = penalty

    def generate_operations(self) -> Iterable[Operation]:
        for submission in self.submissions():
            if parse_timedelta(submission.lateness) > timedelta():
                yield Operation(
                    submission.sid,
                    submission.assignment_id,
                    {},
                )

    def apply(self, context, grade, parameters):
        if grade.score is None:
            return grade
        unwaived_lateness = max(
            parse_timedelta(context.submission.lateness) - grade.waived_lateness,
            timedelta(),
        )
        score = self.penalty(grade.score, unwaived_lateness)
        if score == grade.score:
            return grade
        return grade._replace(
            score=score,
            comments=grade.comments + (
                f"Late penalty: {grade.score:g} -> {score:g} points",
            ),
        )


class SlipDaysPolicy(Policy):
    """Allocate a student's finite slip-day allowance across late submissions."""

    type = "slip_days"

    def __init__(
        self,
        course: "CourseManager",
        id: str,
        priority: int,
        allowance: timedelta,
        assignment_ids: Iterable[str] | None = None,
        assignment_types: Iterable[str] | None = None,
        student_ids: Iterable[str] | None = None,
    ):
        """
        Configure automatic slip-day allocation.

        :param allowance:         lateness available to each student
        :param assignment_ids:    optional assignment IDs to target
        :param assignment_types:  optional assignment types to target
        :param student_ids:       optional student IDs to target
        """
        super().__init__(
            course,
            id,
            priority,
            assignment_ids,
            assignment_types,
            student_ids,
        )
        self.allowance = allowance

    def generate_operations(self) -> Iterable[Operation]:
        submissions_by_student = defaultdict(list)
        for submission in self.submissions():
            submissions_by_student[submission.sid].append(submission)

        for submissions in submissions_by_student.values():
            remaining_lateness = self.allowance
            for submission in sorted(
                submissions,
                key=lambda item: (item.submission_time, item.assignment_id),
            ):
                used_lateness = min(
                    remaining_lateness,
                    parse_timedelta(submission.lateness),
                )
                if used_lateness <= timedelta():
                    continue
                remaining_lateness -= used_lateness
                yield Operation(
                    submission.sid,
                    submission.assignment_id,
                    {'duration': str(used_lateness)},
                )

    def apply(self, context, grade, parameters):
        duration = parse_timedelta(str(parameters['duration']))
        return grade._replace(
            waived_lateness=grade.waived_lateness + duration,
            comments=grade.comments + (f"Slip days used: {duration}",),
        )


class ExtensionPolicy(Policy):
    """Grant additional lateness forgiveness to a policy's target scope."""

    type = "extension"

    def __init__(
        self,
        course: "CourseManager",
        id: str,
        priority: int,
        extension: timedelta,
        student_ids: Iterable[str] | None = None,
        assignment_ids: Iterable[str] | None = None,
    ):
        """
        Configure an extension policy.

        :param extension:       lateness forgiven for each target
        :param student_ids:     optional student IDs to target
        :param assignment_ids:  optional assignment IDs to target
        """
        super().__init__(
            course,
            id,
            priority,
            assignment_ids=assignment_ids,
            student_ids=student_ids,
        )
        self.extension = extension

    def generate_operations(self) -> Iterable[Operation]:
        for submission in self.submissions():
            yield Operation(
                submission.sid,
                submission.assignment_id,
                {'duration': str(self.extension)},
            )

    def apply(self, context, grade, parameters):
        duration = parse_timedelta(str(parameters['duration']))
        return grade._replace(
            waived_lateness=grade.waived_lateness + duration,
            comments=grade.comments + (f"Extension: {duration}",),
        )


class AdjustmentPolicy(Policy):
    """Add a fixed point adjustment to a policy's target scope."""

    type = "score_adjustment"

    def __init__(
        self,
        course: "CourseManager",
        id: str,
        priority: int,
        adjustment: Adjustment,
        reason: str = "",
        assignment_ids: Iterable[str] | None = None,
        assignment_types: Iterable[str] | None = None,
        student_ids: Iterable[str] | None = None,
    ):
        """
        Configure a fixed score adjustment.

        :param adjustment:        adjustment applied to each target grade
        :param reason:            optional explanation for final grade comments
        :param assignment_ids:    optional assignment IDs to target
        :param assignment_types:  optional assignment types to target
        :param student_ids:       optional student IDs to target
        """
        super().__init__(
            course,
            id,
            priority,
            assignment_ids,
            assignment_types,
            student_ids,
        )
        self.adjustment = adjustment
        self.reason = reason

    def generate_operations(self) -> Iterable[Operation]:
        for submission in self.submissions():
            yield Operation(
                submission.sid,
                submission.assignment_id,
                {'adjustment': self.adjustment.to_dict(), 'reason': self.reason},
            )

    def apply(self, context, grade, parameters):
        if grade.score is None:
            return grade
        adjustment_data = parameters['adjustment']
        if not isinstance(adjustment_data, Mapping):
            raise ValueError("score adjustment must be an object")
        adjustment = Adjustment.from_dict(adjustment_data)
        reason = str(parameters.get('reason', ''))
        score = grade.score + adjustment
        comment = f"Score adjustment: {grade.score:g} -> {score:g} points"
        if reason:
            comment += f" ({reason})"
        return grade._replace(
            score=score,
            comments=grade.comments + (comment,),
        )
