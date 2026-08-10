import tempfile
import unittest
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
