import os
from pathlib import Path

import canvasapi

import courseutils.canvas as canvas
import courseutils.db as db
import courseutils.gradescope as gradescope


class CourseManager:

    def __init__(
        self,
        course_id: int,
        db_path: Path,
        root: Path|None = None,
        grading_path: Path|None = None,
    ):
        self.course_id = course_id
        self.db_path = db_path
        self.root = Path.cwd() if not root else root
        self.grading_path = grading_path if grading_path \
            else self.root / "exports"
        self.db_conn = db.open_db(self.db_path)
        self.canvas = canvas.CanvasManager(course_id, self.db_conn)
        self.gradescope = gradescope.GradescopeManager(
            self.grading_path,
            self.db_conn,
        )

