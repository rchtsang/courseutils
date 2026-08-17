import os
import time
import re
import sqlite3 as sql

import canvasapi

from .db import *

STEVENS_SECTION_PTRN = re.compile(
    r"(?P<year>\d{4})"
    r"(?P<session>S|F) "
    r"(?P<subject>\w+) "
    r"(?P<num>\d+)-(?P<sec>\w+)")

class CanvasManager:
    CANVAS_BASE = "https://sit.instructure.com"
    CANVAS_TOKEN = os.environ.get("CANVAS_TOKEN")

    def __init__(
        self,
        course_id: int,
        db_conn: sql.Connection,
    ):
        self.db_conn = db_conn
        self.canvasapi = canvasapi.Canvas(self.CANVAS_BASE, self.CANVAS_TOKEN)
        self.course = self.canvasapi.get_course(course_id)
        if not getattr(self.course, 'apply_assignment_group_weights', False):
            self.course.update(
                course={'apply_assignment_group_weights': True},
            )


    def fetch_student_groups(self) -> dict:
        """
        Loads groups from course and constructs current groupings

        :returns:   dictionary containing groups information
        """
        return {
            g.id: {
                'id': g.id,
                'name': g.name,
                'members': [ u['id'] for u in g.users ],
            } for g in self.course.get_groups(include=['users'])
        }


    def pull_students(self):
        """
        Sync "students" table in sqlite db with current canvas course roster.

        Performs the following:
        1. Students present in the local database, but not on canvas will have
           their status set to 'dropped'
        2. Students present in the local database and canvas will have all data
           updated according to canvas
        3. Students not present in the local database will be added

        Assumptions:
        - Students are only enrolled in 1 canvas section
        - Observer enrollments are auditors
        """
        students = self.course.get_users(
            enrollment_type=['student', 'observer'],
            include=['enrollments'],
        )
        sections = self.course.get_sections(include=['students'])

        students = { u.id: u for u in students }

        assert students, "canvas returned no students!"

        # drop students not found
        self.db_conn.execute(f"""
            UPDATE students
            SET status = 'dropped'
            WHERE canvas_id NOT IN ({', '.join('?' * len(students))})
        """, tuple(students.keys()))

        student_data = {}
        for u in students.values():
            status = "enrolled"
            if u.enrollments[0]['type'] == 'ObserverEnrollment':
                status = "audit"

            student_data[u.id] = dict(
                canvas_id=str(u.id),
                sortable_name=str(u.sortable_name),
                name=str(u.name),
                sid=str(u.sis_user_id),
                email=str(u.email),
                status=status,
            )

        for sec in sections:
            assert (m := STEVENS_SECTION_PTRN.search(sec.name)), \
                "invalid section name: {}".format(sec.name)
            section_name = m.group('sec')
            key = "lab_section_id" if "L" in section_name else "section_id"

            for student in sec.students:
                student_data[student['id']][key] = sec.id

        assert student_data.keys() == students.keys(), "values to insert != students"

        update_table('students', self.db_conn, list(student_data.values()), ['canvas_id'])


    def upload_grades(
        self,
        assignment_id: str,
    ) -> canvasapi.progress.Progress:
        """
        Uploads grades from database to canvas

        :param      assignment_id:  the assignment id
        :type       assignment_id:  str
        :returns:   a progress object
        :rtype:     canvasapi.progress.Progress
        """

        grades_cmd = """
            SELECT s.canvas_id, g.score, g.comments
            FROM students s
            JOIN grades g
            ON s.sid = g.sid
            WHERE g.assignment_id = ?
        """
        grade_data = {
            r['canvas_id']: { 'posted_grade': r['score'], 'text_comment': r['comments'] } \
            for r in self.db_conn.execute(grades_cmd, (assignment_id,)).fetchall()
        }

        canvas_cmd = "SELECT canvas_id FROM assignments WHERE id = ?"
        assert (res := self.db_conn.execute(canvas_cmd, (assignment_id,)).fetchone()), \
            "could not find {}".format(assignment_id)

        canvas_id = int(res['canvas_id'])
        assignment = self.course.get_assignment(canvas_id)

        progress = assignment.submissions_bulk_update(grade_data=grade_data)
        while not progress.completion:
            print(str(progress))
            time.sleep(1)
            progress = progress.query()

        return progress

    def create_assignment_group(
        self,
        type: str,
        weight: float | None = None,
        **kwargs,
    ):
        """Create an assignment group in Canvas and record it locally.

        :param type: assignment type represented by the group
        :param weight: optional percentage of the final grade for this group
        :param kwargs: additional Canvas assignment-group fields
        :returns: the Canvas assignment-group resource
        """
        fields = { 'name': type.title() }
        if weight is not None:
            fields['group_weight'] = weight
        fields.update(kwargs)

        weight = fields.get('group_weight')

        for g in self.course.get_assignment_groups():
            if g.name != fields['name']:
                continue
            if weight is not None and g.group_weight != weight:
                raise ValueError(
                    f"Canvas assignment group {type} has weight "
                    f"{g.group_weight}, not {weight}"
                )
            return g

        assert not self.db_conn.execute(
            "SELECT 1 FROM assignment_groups WHERE type = ?",
            (type,),
        ).fetchone(), \
            "assignment group already exists: {}".format(type)

        group = self.course.create_assignment_group(**fields)
        weight = getattr(group, 'group_weight', weight)
        self.db_conn.execute("""
            INSERT INTO assignment_groups (type, canvas_id, weight)
            VALUES (?, ?, ?)
        """, (type, group.id, weight))

        return group

    def create_assignment(
        self,
        id: str,
        title: str,
        assignment_type: str,
        max_points: float|int,
        gradescope_id: int|None,
        **kwargs,
    ):
        """
        Create a new assignment in the database and add it to canvas

        :param      id:             local db assignment id
        :type       id:             str
        :param      title:          assignment title
        :type       title:          str
        :param      assignment_type: assignment type
        :type       assignment_type: str
        :param      max_points:     assignment point value
        :type       max_points:     float|int
        :param      gradescope_id:  optional assignment id on gradescope
        :type       gradescope_id:  int|None
        """
        fields = {
            'name': title,
            'points_possible': max_points,
        }
        fields.update(kwargs)
        assignment = self.course.create_assignment(assignment=fields)

        self.db_conn.execute("""
            INSERT INTO assignments
              (id, canvas_id, gradescope_id, title, type, max_points)
            VALUES
              (?, ?, ?, ?, ?, ?)
        """, (
            id,
            assignment.id,
            gradescope_id,
            title,
            assignment_type,
            max_points,
        ))

        return assignment

    def delete_assignment(self, id: str):
        """Delete an assignment from Canvas and the local database.

        The Canvas assignment is deleted before its local record so a remote
        failure leaves the local assignment and its related grading data intact.

        :param id: local database assignment ID
        """
        row = self.db_conn.execute(
            "SELECT canvas_id FROM assignments WHERE id = ?",
            (id,),
        ).fetchone()
        assert row, "could not find {}".format(id)

        assignment = self.course.get_assignment(int(row['canvas_id']))
        assignment.delete()
        self.db_conn.execute("DELETE FROM assignments WHERE id = ?", (id,))
