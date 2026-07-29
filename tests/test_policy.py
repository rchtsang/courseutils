import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from courseutils import CourseManager
from courseutils.models import Grade, GradeOperation, Operation
from courseutils.policy import (
    AdjustmentPolicy,
    Adjustment,
    ExtensionPolicy,
    LatePenaltyPolicy,
    Policy,
    SlipDaysPolicy,
)


class ScoreScalePolicy(Policy):
    type = "score_scale"

    def __init__(self, *args, factor: float, **kwargs):
        super().__init__(*args, **kwargs)
        self.factor = factor

    def generate_operations(self):
        for submission in self.submissions():
            yield Operation(
                submission.sid,
                submission.assignment_id,
                {'factor': self.factor},
            )

    def apply(self, context, grade, parameters):
        if grade.score is None:
            return grade
        return grade._replace(
            score=grade.score * float(parameters['factor']),
            comments=grade.comments + ("Custom score scale",),
        )


class PolicyPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.course = CourseManager(1, Path(self.tempdir.name) / "course.db")
        self.course.db_conn.execute("""
            INSERT INTO students
              (sid, canvas_id, sortable_name, name, email, section_id,
               lab_section_id, status)
            VALUES
              ('student-1', 1, 'Student, One', 'Student One',
               'student@example.edu', NULL, NULL, 'enrolled')
        """)
        self.course.db_conn.executemany("""
            INSERT INTO assignments
              (id, canvas_id, gradescope_id, title, type, max_points)
            VALUES
              (?, ?, ?, ?, 'homework', 100)
        """, [
            ('hw1', 1, 1, 'Homework 1'),
            ('hw2', 2, 2, 'Homework 2'),
        ])
        self.course.db_conn.executemany("""
            INSERT INTO submissions
              (sid, assignment_id, total_score, status, submission_id,
               submission_time, lateness, view_count, submission_count,
               last_updated)
            VALUES
              ('student-1', ?, 100, 'Graded', ?, ?, ?, 0, 1, '2026-01-01')
        """, [
            ('hw1', 1, '2026-01-01T12:00:00', '12:00:00'),
            ('hw2', 2, '2026-01-02T12:00:00', '36:00:00'),
        ])

    def tearDown(self):
        self.course.db_conn.close()
        self.tempdir.cleanup()

    def test_build_grades_rebuilds_generated_operations(self):
        self.course.register_policies([
            SlipDaysPolicy(
                self.course,
                id='slip-days',
                priority=10,
                allowance=timedelta(days=1),
                assignment_types=('homework',),
            ),
            LatePenaltyPolicy(
                self.course,
                id='late-penalty',
                priority=20,
                penalty=lambda score, lateness: score - 10 * (
                    lateness / timedelta(days=1)
                ),
                assignment_types=('homework',),
            ),
            AdjustmentPolicy(
                self.course,
                id='adjustment-handler',
                priority=30,
                adjustment=Adjustment(points=0),
                student_ids=('not-a-student',),
            ),
        ])
        self.course.add_operation(
            'student-1',
            'hw2',
            'score_adjustment',
            30,
            {
                'adjustment': Adjustment(points=-5).to_dict(),
                'reason': 'manual penalty',
            },
        )

        self.course.build_grades()

        grades = {
            (grade.sid, grade.assignment_id): grade
            for grade in self.course.get_rows(Grade)
        }
        self.assertEqual(grades[('student-1', 'hw1')].score, 100)
        self.assertEqual(grades[('student-1', 'hw2')].score, 85)
        self.assertIn('manual penalty', grades[('student-1', 'hw2')].comments)

        self.course.build_grades()

        operations = self.course.get_rows(GradeOperation)
        self.assertEqual(len(operations), 5)
        self.assertEqual(sum(operation.policy is None for operation in operations), 1)
        grades = {
            (grade.sid, grade.assignment_id): grade
            for grade in self.course.get_rows(Grade)
        }
        self.assertEqual(grades[('student-1', 'hw2')].score, 85)

    def test_custom_policy_generates_and_resolves_operations(self):
        self.course.register_policies([
            ScoreScalePolicy(
                self.course,
                id='half-credit',
                priority=10,
                factor=0.5,
                assignment_ids=('hw1',),
            ),
        ])

        self.course.build_grades()

        grades = {
            (grade.sid, grade.assignment_id): grade
            for grade in self.course.get_rows(Grade)
        }
        self.assertEqual(grades[('student-1', 'hw1')].score, 50)
        self.assertEqual(grades[('student-1', 'hw2')].score, 100)

    def test_adjustment_supports_points_and_percentages(self):
        self.assertEqual(80 + Adjustment(points=-5), 75)
        self.assertEqual(80 + Adjustment(percent=-10), 72)

    def test_extension_targets_specific_student_and_assignment(self):
        self.course.register_policies([
            ExtensionPolicy(
                self.course,
                id='hw1-extension',
                priority=10,
                extension=timedelta(days=1),
                student_ids=('student-1',),
                assignment_ids=('hw1',),
            ),
            LatePenaltyPolicy(
                self.course,
                id='late-penalty',
                priority=20,
                penalty=lambda score, lateness: score + Adjustment(
                    percent=max(
                        -50,
                        -20 * (lateness / timedelta(days=1)),
                    ),
                ),
            ),
        ])

        self.course.build_grades()

        grades = {
            (grade.sid, grade.assignment_id): grade
            for grade in self.course.get_rows(Grade)
        }
        self.assertEqual(grades[('student-1', 'hw1')].score, 100)
        self.assertEqual(grades[('student-1', 'hw2')].score, 70)


if __name__ == '__main__':
    unittest.main()
