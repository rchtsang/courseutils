import re
import sqlite3 as sql
import csv
from datetime import datetime
from pathlib import Path

from .db import *

QUESTION_NAME_PTRN = re.compile(
    r"(?P<q>\d+(?:\.\d+)?)(?::(?P<name>.+))?\s*"
    r"\((?P<points>\d+(?:\.\d+)?) pts\)")

EVAL_FILENAME_PTRN = re.compile(
    r"(?P<q>\d+(?:\.\d+)?)(?:_(?P<title>.+))?\.csv")

def _map_colname(field: str):
    if (m := QUESTION_NAME_PTRN.search(field)):
        return f"q{m.group('q')}"
    elif "(H:M:S)" in field:
        return field.replace("(H:M:S)", '').strip().replace(' ', '_').lower()
    else:
        return field.lower().replace(' ', '_')

_caststr = lambda s: str(s) if s else ''
_castint = lambda s: int(s, 0) if s else 0
_castflt = lambda s: float(s) if s else 0.0

GRADES_COMMON_COLS = [
    ("First Name", _caststr),
    ("Last Name", _caststr),
    ("SID", _caststr),
    ("Email", _caststr),
    ("Sections", _caststr),
    ("Total Score", _castflt),
    ("Max Points", _castflt),
    ("Status", _caststr),
    ("Submission ID", lambda s: int(s, 0) if s else None),
    ("Submission Time", _caststr),
    ("Lateness (H:M:S)", _caststr),
    ("View Count", _castint),
    ("Submission Count", _castint),
]

GRADES_COLNAME_MAPPING = {
    _map_colname(c): cast_fn for c, cast_fn in GRADES_COMMON_COLS
}

RUBRIC_COMMON_COLS = [
    ("Assignment Submission ID", _caststr),
    ("Question Submission ID", _caststr),
    ("First Name", _caststr),
    ("Last Name", _caststr),
    ("SID", _caststr),
    ("Email", _caststr),
    ("Sections", _caststr),
    ("Score", _castflt),
    ("Submission Time", _caststr),
    ("Adjustment", _castflt),
    ("Comments", _caststr),
    ("Grader", _caststr),
    ("Tags", _caststr),
]

RUBRIC_COLNAME_MAPPING = {
    _map_colname(c): cast_fn for c, cast_fn in RUBRIC_COMMON_COLS
}

GRADES_TBL_FIELDS = list(GRADES_COLNAME_MAPPING.keys())
GRADES_TBL_FIELDS.insert(4, "assignment")


class GradescopeManager:

    def __init__(
        self,
        grading_path: Path,
        db_conn: sql.Connection,
    ):
        assert grading_path.is_dir(), \
            "directory not found: {}".format(str(grading_path))

        self.grading_path = grading_path
        self.db_conn = db_conn


    def _process_scores(self, assignment_id: str) -> dict[str,list[dict]]:
        """
        Process gradescope-formatted csv scores file

        :param      assignment_id:      assignment id to load
        :type       assignment_id:      str
        :returns:   extracted questions and submissions data
        :rtype:     dict[str,list[dict]]
        """
        path = self.grading_path / f"scores-{assignment_id}.csv"
        assert path.exists(), "file not found: {}".format(str(path))

        def coltype(k, v):
            if k in GRADES_COLNAME_MAPPING:
                return GRADES_COLNAME_MAPPING[k](v)
            else:
                return float(v) if v else 0.0

        tables = get_tablenames(self.db_conn)

        questions = []
        submissions = []

        last_updated = datetime.now().isoformat()

        with open(path, 'r') as f:
            assert (fieldnames := csv.DictReader(f).fieldnames) is not None, \
                "could not find fieldnames: {}".format(str(path))

            for fieldname in fieldnames:
                if not (m := QUESTION_NAME_PTRN.search(fieldname)):
                    continue
                questions.append(dict(
                    id=f"{assignment_id}_q{m.group('q')}",
                    assignment_id=assignment_id,
                    title=m.group('name'),
                    max_points=float(m.group('points')),
                ))

            fieldnames = [ _map_colname(field) for field in fieldnames ]
            reader = csv.DictReader(f, fieldnames=fieldnames)

            for row in reader:
                submission = { k: coltype(k, v) for k, v in row.items() }
                submission['assignment_id'] = assignment_id
                submission['last_updated'] = last_updated

                submissions.append({
                    k: v for k, v in submission.items() \
                    if k in tables['submissions']
                })

        return { 'questions': questions, 'submissions': submissions }


    def _process_evaluation(self, path: Path, assignment_id: str) -> dict[str,list[dict]]:
        assert path.exists(), "file not found: {}".format(str(path))
        assert (m := EVAL_FILENAME_PTRN.search(path.name)), \
            "invalid naming convention: {}".format(str(path))

        question_id = f"{assignment_id}_q{m.group('q')}"

        def coltype(k, v):
            if k in RUBRIC_COLNAME_MAPPING:
                return RUBRIC_COLNAME_MAPPING[k](v)
            else:
                return str(v) if v else ''

        entries = []
        with open(path, 'r') as f:
            fieldnames = []
            assert (_fieldnames := csv.DictReader(f).fieldnames) is not None, \
                "could not find fieldnames: {}".format(str(path))

            for field in _fieldnames:
                colname = _map_colname(field)
                if colname in RUBRIC_COLNAME_MAPPING:
                    fieldnames.append(colname)
                else:
                    fieldnames.append(field)

            reader = csv.DictReader(f, fieldnames=fieldnames)
            for row in reader:
                if row[fieldnames[0]] == "Point Values":
                    break
                entries.append({ k: coltype(k, v) for k, v in row.items() })

        rubric_keys = {
            k: i for i, k in enumerate([
                k for k in fieldnames if k not in RUBRIC_COLNAME_MAPPING
            ])
        }
        rubric_items = {}
        for key, i in rubric_keys.items():
            rubric_id = f"{question_id}_r{i}"
            rubric_items[rubric_id] = {
                'id': rubric_id,
                'assignment_id': assignment_id,
                'question_id': question_id,
                'rubric_idx': i,
                'description': key,
            }

        question_scores_keys = [
            'sid', 'score', 'adjustment', 'comments', 'grader', 'tags'
        ]
        question_scores = []
        rubric_items_applied = []

        for entry in entries:
            question_scores.append(dict([
                (k, entry[k]) for k in question_scores_keys
            ] + [
                ('question_id', question_id),
            ]))

            for rubric_id, rubric_info in rubric_items.items():
                if entry[rubric_info['description']] != "true":
                    continue
                rubric_items_applied.append(dict(
                    sid=entry['sid'],
                    assignment_id=assignment_id,
                    rubric_id=rubric_id,
                ))

        return {
            'rubric_items': list(rubric_items.values()),
            'question_scores': question_scores,
            'rubric_items_applied': rubric_items_applied,
        }

    def _process_evaluations(self, assignment_id: str) -> dict[str,list[dict]]:
        """
        Process gradescope-exported evaluations for an assignment

        :param      assignment_id:      assignment identifier
        :type       assignment_id:      str
        :returns:   extracted rubric items and question scores
        :rtype:     dict[str,list[dict]]
        """
        directory = self.grading_path / f"evaluations-{assignment_id}"
        assert directory.is_dir(), "not a directory: {}".format(str(directory))

        evaluations = list(directory.glob("*.csv"))

        question_scores = []
        rubric_items = []
        rubric_items_applied = []

        for path in evaluations:
            extracted = self._process_evaluation(path, assignment_id)
            question_scores.extend(extracted['question_scores'])
            rubric_items.extend(extracted['rubric_items'])
            rubric_items_applied.extend(extracted['rubric_items_applied'])

        return {
            'question_scores': question_scores,
            'rubric_items': rubric_items,
            'rubric_items_applied': rubric_items_applied,
        }

    def load_assignment_data(self, assignment_id: str):
        """
        Process assignment data from exported gradescope evaluations and scores
        and update local database tables

        :param      assignment_id:      the assignment to process
        :type       assignment_id:      str
        """
        self.db_conn.execute("SAVEPOINT load_assignment_data")
        try:
            data = {}
            data.update(self._process_scores(assignment_id))
            data.update(self._process_evaluations(assignment_id))

            update_table(
                'questions',
                self.db_conn,
                data['questions'],
                ['id'],
            )
            update_table(
                'submissions',
                self.db_conn,
                data['submissions'],
                ['sid', 'assignment_id'],
            )
            update_table(
                'question_scores',
                self.db_conn,
                data['question_scores'],
                ['sid', 'question_id'],
            )
            update_table(
                'rubric_items',
                self.db_conn,
                data['rubric_items'],
                ['id'],
            )

            # rubric_items_applied is different, since it is purely
            # relational.
            # all data for the current assignment needs to be
            # overwritten entirely.
            self.db_conn.execute("""
                DELETE FROM rubric_items_applied
                WHERE assignment_id = ?
            """, (assignment_id,))
            self.db_conn.executemany("""
                INSERT INTO rubric_items_applied
                  (sid, assignment_id, rubric_id)
                VALUES
                  (:sid, :assignment_id, :rubric_id)
            """, data['rubric_items_applied'])
        except Exception:
            self.db_conn.execute(
                "ROLLBACK TO SAVEPOINT load_assignment_data")
            raise
        finally:
            self.db_conn.execute(
                "RELEASE SAVEPOINT load_assignment_data")

