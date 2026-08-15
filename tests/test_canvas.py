import tempfile
import unittest
import sqlite3
from pathlib import Path

from courseutils.canvas import CanvasManager
from courseutils.db import open_db


class FakeAssignment:
    def __init__(self, error=None):
        self.deleted = False
        self.error = error

    def delete(self):
        if self.error:
            raise self.error
        self.deleted = True


class FakeCourse:
    def __init__(self, assignment):
        self.assignment = assignment
        self.assignment_id = None

    def get_assignment(self, assignment_id):
        self.assignment_id = assignment_id
        return self.assignment


class FakeCreatedAssignment:
    id = 789


class FakeAssignmentCreator:
    def __init__(self):
        self.data = None

    def create_assignment(self, assignment):
        self.data = assignment
        return FakeCreatedAssignment()


class FakeCreatedAssignmentGroup:
    id = 987
    group_weight = 25


class FakeExistingAssignmentGroup:
    def __init__(self, id, name, group_weight):
        self.id = id
        self.name = name
        self.group_weight = group_weight


class FakeAssignmentGroupCreator:
    def __init__(self, groups=()):
        self.data = None
        self.groups = groups

    def get_assignment_groups(self):
        return self.groups

    def create_assignment_group(self, **kwargs):
        self.data = kwargs
        return FakeCreatedAssignmentGroup()


class DeleteAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_conn = open_db(Path(self.tempdir.name) / "course.db")
        self.db_conn.execute("""
            INSERT INTO assignments
              (id, canvas_id, gradescope_id, title, type, max_points)
            VALUES
              ('hw1', 123, 456, 'Homework 1', 'homework', 100)
        """)
        self.canvas = CanvasManager.__new__(CanvasManager)
        self.canvas.db_conn = self.db_conn

    def tearDown(self):
        self.db_conn.close()
        self.tempdir.cleanup()

    def test_delete_assignment_removes_canvas_and_local_records(self):
        assignment = FakeAssignment()
        course = FakeCourse(assignment)
        self.canvas.course = course

        self.canvas.delete_assignment('hw1')

        self.assertEqual(course.assignment_id, 123)
        self.assertTrue(assignment.deleted)
        self.assertIsNone(self.db_conn.execute(
            "SELECT 1 FROM assignments WHERE id = 'hw1'"
        ).fetchone())

    def test_delete_assignment_keeps_local_record_when_canvas_delete_fails(self):
        assignment = FakeAssignment(error=RuntimeError('Canvas unavailable'))
        self.canvas.course = FakeCourse(assignment)

        with self.assertRaisesRegex(RuntimeError, 'Canvas unavailable'):
            self.canvas.delete_assignment('hw1')

        self.assertIsNotNone(self.db_conn.execute(
            "SELECT 1 FROM assignments WHERE id = 'hw1'"
        ).fetchone())


class GradescopeAssignmentIdTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_conn = open_db(Path(self.tempdir.name) / "course.db")

    def tearDown(self):
        self.db_conn.close()
        self.tempdir.cleanup()

    def test_assignments_allow_multiple_missing_gradescope_ids(self):
        self.db_conn.executemany("""
            INSERT INTO assignments
              (id, canvas_id, gradescope_id, title, type, max_points)
            VALUES (?, ?, NULL, ?, 'lab', 10)
        """, [
            ('lab0', 1, 'Lab 0'),
            ('lab1', 2, 'Lab 1'),
        ])

        rows = self.db_conn.execute(
            "SELECT gradescope_id FROM assignments ORDER BY id"
        ).fetchall()
        self.assertEqual([row['gradescope_id'] for row in rows], [None, None])

    def test_assignments_reject_duplicate_gradescope_ids(self):
        self.db_conn.execute("""
            INSERT INTO assignments
              (id, canvas_id, gradescope_id, title, type, max_points)
            VALUES ('hw0', 1, 123, 'Homework 0', 'homework', 100)
        """)

        with self.assertRaises(sqlite3.IntegrityError):
            self.db_conn.execute("""
                INSERT INTO assignments
                  (id, canvas_id, gradescope_id, title, type, max_points)
                VALUES ('hw1', 2, 123, 'Homework 1', 'homework', 100)
            """)

    def test_canvas_creation_accepts_missing_gradescope_id(self):
        canvas = CanvasManager.__new__(CanvasManager)
        canvas.db_conn = self.db_conn
        canvas.course = FakeAssignmentCreator()

        canvas.create_assignment('lab0', 'Lab 0', 'lab', 10, None)

        row = self.db_conn.execute(
            "SELECT gradescope_id FROM assignments WHERE id = 'lab0'"
        ).fetchone()
        self.assertIsNone(row['gradescope_id'])


class AssignmentGroupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_conn = open_db(Path(self.tempdir.name) / "course.db")
        self.canvas = CanvasManager.__new__(CanvasManager)
        self.canvas.db_conn = self.db_conn
        self.canvas.course = FakeAssignmentGroupCreator()

    def tearDown(self):
        self.db_conn.close()
        self.tempdir.cleanup()

    def test_create_assignment_group_records_canvas_group(self):
        group = self.canvas.create_assignment_group(
            'homework',
            25,
            rules={'drop_lowest': 1},
        )

        self.assertEqual(group.id, 987)
        self.assertEqual(self.canvas.course.data, {
            'name': 'Homework',
            'group_weight': 25,
            'rules': {'drop_lowest': 1},
        })
        row = self.db_conn.execute(
            "SELECT type, canvas_id, weight FROM assignment_groups"
        ).fetchone()
        self.assertEqual(dict(row), {
            'type': 'homework',
            'canvas_id': 987,
            'weight': 25,
        })

    def test_create_assignment_group_reuses_existing_canvas_group(self):
        existing_group = FakeExistingAssignmentGroup(654, 'Homework', 30)
        self.canvas.course = FakeAssignmentGroupCreator([existing_group])

        group = self.canvas.create_assignment_group('homework', 25)

        self.assertIs(group, existing_group)
        self.assertIsNone(self.canvas.course.data)
        row = self.db_conn.execute(
            "SELECT canvas_id, weight FROM assignment_groups"
        ).fetchone()
        self.assertEqual(dict(row), {'canvas_id': 654, 'weight': 30})
