import json
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping, TypeVar

from . import db
from .models import (
    Assignment,
    Grade,
    GradeOperation,
    Question,
    QuestionScore,
    RubricItem,
    RubricItemApplied,
    Student,
    Submission,
    TABLE_NAMES,
)
from .policy import GradeContext, GradeState, Policy

if TYPE_CHECKING:
    from .canvas import CanvasManager
    from .gradescope import GradescopeManager


RowT = TypeVar("RowT")


class CourseManager:
    """Coordinate local course data, remote services, and grade policies."""

    def __init__(
        self,
        course_id: int,
        db_path: Path,
        root: Path|None = None,
        grading_path: Path|None = None,
    ):
        """
        Open a course database without contacting remote services.

        Canvas and Gradescope managers are initialized only when their
        corresponding properties are accessed.

        :param course_id:     Canvas course identifier
        :param db_path:       local SQLite database path
        :param root:          base path for course files
        :param grading_path:  Gradescope export directory
        """
        self.course_id = course_id
        self.db_path = db_path
        self.root = Path.cwd() if not root else root
        self.grading_path = grading_path if grading_path \
            else self.root / "exports"
        self.db_conn = db.open_db(self.db_path)
        self._canvas = None
        self._gradescope = None
        self._policies: list[Policy] = []

    @property
    def canvas(self) -> "CanvasManager":
        """Return the Canvas manager, initializing it on first access."""
        if self._canvas is None:
            from .canvas import CanvasManager
            self._canvas = CanvasManager(self.course_id, self.db_conn)
        return self._canvas

    @property
    def gradescope(self) -> "GradescopeManager":
        """Return the Gradescope manager, initializing it on first access."""
        if self._gradescope is None:
            from .gradescope import GradescopeManager
            self._gradescope = GradescopeManager(
                self.grading_path,
                self.db_conn,
            )
        return self._gradescope

    def get_rows(self, row_type: type[RowT]) -> list[RowT]:
        """
        Read all rows for a typed database model.

        :param row_type: a named-tuple model from ``courseutils.models``
        :returns: rows decoded into instances of ``row_type``
        """
        try:
            table = TABLE_NAMES[row_type]
        except KeyError as error:
            raise ValueError(f"unsupported row type: {row_type}") from error
        return [
            row_type(**dict(row))
            for row in self.db_conn.execute(f"SELECT * FROM {table}")
        ]

    def register_policies(self, policies: Iterable[Policy]):
        """
        Register policies used by subsequent calls to :meth:`build_grades`.

        Policies are held in stable ascending priority order. Every policy
        instance must belong to this course manager and have a unique ID.

        :param policies: instantiated policy objects
        """
        policies = list(policies)
        if any(policy.course is not self for policy in policies):
            raise ValueError("policy belongs to a different course manager")
        if len({policy.id for policy in policies}) != len(policies):
            raise ValueError("policy IDs must be unique")
        self._policies = sorted(policies, key=lambda policy: policy.priority)

    def add_operation(
        self,
        sid: str,
        assignment_id: str,
        type: str,
        priority: int,
        parameters: Mapping[str, object],
    ) -> int:
        """
        Persist a manually entered grade operation.

        Manual operations have no generating policy and survive policy
        regeneration. The operation type must be handled by a registered
        policy before :meth:`build_grades` can resolve it.

        :param sid:            student ID
        :param assignment_id:  local assignment ID
        :param type:           operation type to apply
        :param priority:       lower values apply first
        :param parameters:     JSON-serializable operation configuration
        :returns: inserted operation ID
        """
        cursor = self.db_conn.execute("""
            INSERT INTO grade_operations
              (sid, assignment_id, type, priority, parameters, policy)
            VALUES
              (?, ?, ?, ?, ?, NULL)
        """, (
            sid,
            assignment_id,
            type,
            priority,
            json.dumps(parameters, sort_keys=True),
        ))
        return cursor.lastrowid

    def build_grades(self):
        """
        Regenerate policy operations and final grades in a savepoint.

        Generated operations are replaced, manual operations are retained,
        and every final grade is rebuilt from imported source data. This
        method does not commit the surrounding database transaction.
        """
        policies_by_id = {policy.id: policy for policy in self._policies}
        policies_by_type: dict[str, Policy] = {}
        for policy in self._policies:
            existing = policies_by_type.get(policy.type)
            if existing is not None and type(existing) is not type(policy):
                raise ValueError(
                    f"multiple policy classes handle operation type {policy.type}"
                )
            policies_by_type.setdefault(policy.type, policy)

        self.db_conn.execute("SAVEPOINT build_grades")
        try:
            self.db_conn.execute(
                "DELETE FROM grade_operations WHERE policy IS NOT NULL")
            for policy in self._policies:
                self.db_conn.executemany("""
                    INSERT INTO grade_operations
                      (sid, assignment_id, type, priority, parameters, policy)
                    VALUES
                      (?, ?, ?, ?, ?, ?)
                """, [
                    (
                        operation.sid,
                        operation.assignment_id,
                        policy.type,
                        policy.priority,
                        json.dumps(operation.parameters, sort_keys=True),
                        policy.id,
                    )
                    for operation in policy.generate_operations()
                ])

            students = {student.sid: student for student in self.get_rows(Student)}
            assignments = {
                assignment.id: assignment
                for assignment in self.get_rows(Assignment)
            }
            questions_by_assignment = defaultdict(list)
            for question in self.get_rows(Question):
                questions_by_assignment[question.assignment_id].append(question)
            question_scores_by_student = defaultdict(list)
            for question_score in self.get_rows(QuestionScore):
                question_scores_by_student[question_score.sid].append(question_score)
            rubric_items_by_assignment = defaultdict(list)
            for rubric_item in self.get_rows(RubricItem):
                rubric_items_by_assignment[rubric_item.assignment_id].append(rubric_item)
            rubric_items_applied_by_student = defaultdict(list)
            for rubric_item in self.get_rows(RubricItemApplied):
                rubric_items_applied_by_student[rubric_item.sid].append(rubric_item)
            operations_by_submission = defaultdict(list)
            for operation in self.get_rows(GradeOperation):
                operations_by_submission[
                    (operation.sid, operation.assignment_id)
                ].append(operation)

            grades = []
            for submission in self.get_rows(Submission):
                questions = tuple(questions_by_assignment[submission.assignment_id])
                question_ids = {question.id for question in questions}
                context = GradeContext(
                    student=students[submission.sid],
                    assignment=assignments[submission.assignment_id],
                    submission=submission,
                    questions=questions,
                    question_scores=tuple(
                        score
                        for score in question_scores_by_student[submission.sid]
                        if score.question_id in question_ids
                    ),
                    rubric_items=tuple(
                        rubric_items_by_assignment[submission.assignment_id]
                    ),
                    rubric_items_applied=tuple(
                        rubric_item
                        for rubric_item in rubric_items_applied_by_student[submission.sid]
                        if rubric_item.assignment_id == submission.assignment_id
                    ),
                )
                grade = GradeState(score=submission.total_score)
                for operation in sorted(
                    operations_by_submission[(submission.sid, submission.assignment_id)],
                    key=lambda item: (item.priority, item.id),
                ):
                    if operation.policy is None:
                        policy = policies_by_type.get(operation.type)
                    else:
                        policy = policies_by_id.get(operation.policy)
                    if policy is None:
                        raise ValueError(
                            f"no registered policy handles operation {operation.id}"
                        )
                    if policy.type != operation.type:
                        raise ValueError(
                            f"policy {policy.id} cannot handle operation {operation.id}"
                        )
                    parameters = json.loads(operation.parameters)
                    if not isinstance(parameters, dict):
                        raise ValueError(
                            f"operation {operation.id} parameters must be an object"
                        )
                    grade = policy.apply(context, grade, parameters)
                grades.append(Grade(
                    sid=submission.sid,
                    assignment_id=submission.assignment_id,
                    score=grade.score,
                    comments='\n'.join(grade.comments) or None,
                ))

            self.db_conn.execute("DELETE FROM grades")
            self.db_conn.executemany("""
                INSERT INTO grades (sid, assignment_id, score, comments)
                VALUES (?, ?, ?, ?)
            """, grades)
        except Exception:
            self.db_conn.execute("ROLLBACK TO SAVEPOINT build_grades")
            raise
        finally:
            self.db_conn.execute("RELEASE SAVEPOINT build_grades")
