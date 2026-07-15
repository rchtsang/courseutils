import os
import time
import sqlite3 as sql

import canvasapi

from .db import *

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


    def fetch_groups(self) -> dict:
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

        data = {}
        for sec in sections:
            for student in sec.students:
                u = students[student['id']]
                status = "enrolled"
                if u.enrollments[0]['type'] == 'ObserverEnrollment':
                    status = "audit"

                data[u.id] = dict(
                    canvas_id=str(u.id),
                    sortable_name=str(u.sortable_name),
                    name=str(u.name),
                    sid=str(u.sis_user_id),
                    email=str(u.email),
                    section_id=str(sec.id),
                    lab_section_id=None, # TODO: figure out how to manage this gracefully
                    status=status,
                )

        assert data.keys() == students.keys(), "values to insert != students"

        update_table('students', self.db_conn, list(data.values()), ['canvas_id'])


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

