PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;

-- canvas course sections
CREATE TABLE IF NOT EXISTS sections (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  time TEXT NOT NULL,
  location TEXT NOT NULL
);

-- lab sections
CREATE TABLE IF NOT EXISTS lab_sections (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  time TEXT NOT NULL,
  location TEXT NOT NULL
);

-- student information
CREATE TABLE IF NOT EXISTS students (
  sid TEXT PRIMARY KEY,
  canvas_id INTEGER NOT NULL UNIQUE,
  sortable_name TEXT NOT NULL,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  section_id INTEGER,
  lab_section_id INTEGER,
  status TEXT NOT NULL
    CHECK (status IN ('enrolled', 'audit', 'dropped')),

  FOREIGN KEY (section_id) REFERENCES sections (id)
    ON UPDATE CASCADE
    ON DELETE SET NULL,

  FOREIGN KEY (lab_section_id) REFERENCES lab_sections (id)
    ON UPDATE CASCADE
    ON DELETE SET NULL
);

-- define and link assignments to canvas/gradescope
CREATE TABLE IF NOT EXISTS assignments (
  id TEXT PRIMARY KEY,
  canvas_id INTEGER NOT NULL UNIQUE,
  gradescope_id INTEGER NOT NULL UNIQUE,
  title TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL CHECK (type IN (
    'homework', 'quiz', 'exam', 'project', 'final', 'midterm', 'lab', 'other'
  )),
  max_points REAL
);

-- exported gradescope submissions
CREATE TABLE IF NOT EXISTS submissions (
  sid TEXT NOT NULL,
  assignment_id TEXT NOT NULL,
  total_score REAL,
  status TEXT NOT NULL CHECK (status IN ('Graded', 'Ungraded', 'Missing')),
  submission_id INTEGER,
  submission_time TEXT NOT NULL,
  lateness TEXT NOT NULL,
  view_count INTEGER,
  submission_count INTEGER,
  last_updated TEXT NOT NULL,

  PRIMARY KEY (sid, assignment_id),

  FOREIGN KEY (sid) REFERENCES students (sid)
    ON UPDATE CASCADE
    ON DELETE CASCADE,

  FOREIGN KEY (assignment_id) REFERENCES assignments (id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
);
-- enforce uniqueness on non-null submission_ids
CREATE UNIQUE INDEX IF NOT EXISTS unique_submission_id
  ON submissions (submission_id)
  WHERE submission_id IS NOT NULL;

-- final assignment grades after processing submissions
CREATE TABLE IF NOT EXISTS grades (
  sid TEXT NOT NULL,
  assignment_id TEXT NOT NULL,
  score REAL,
  comments TEXT,

  PRIMARY KEY (sid, assignment_id),

  FOREIGN KEY (sid, assignment_id) REFERENCES submissions (sid, assignment_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
);

-- questions derived from gradscope export
CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY,
  assignment_id TEXT NOT NULL,
  title TEXT,
  max_points REAL NOT NULL,

  UNIQUE (id, assignment_id),

  FOREIGN KEY (assignment_id) REFERENCES assignments (id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
);

-- exported rubric item from gradescope for all assignments
CREATE TABLE IF NOT EXISTS rubric_items (
  id TEXT PRIMARY KEY,
  assignment_id TEXT NOT NULL,
  question_id TEXT NOT NULL,
  rubric_idx INTEGER NOT NULL,
  description TEXT,

  UNIQUE (id, assignment_id),
  UNIQUE (assignment_id, question_id, rubric_idx),

  FOREIGN KEY (question_id, assignment_id) REFERENCES questions (id, assignment_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
);

-- question scores derived from gradescope export
CREATE TABLE IF NOT EXISTS question_scores (
  sid TEXT NOT NULL,
  question_id TEXT NOT NULL,
  score REAL NOT NULL,
  adjustment REAL,
  comments TEXT,
  grader TEXT,
  tags TEXT,

  PRIMARY KEY (sid, question_id),

  FOREIGN KEY (sid) REFERENCES students (sid)
    ON UPDATE CASCADE
    ON DELETE CASCADE,

  FOREIGN KEY (question_id) REFERENCES questions (id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
);

-- rubric items applied to students
CREATE TABLE IF NOT EXISTS rubric_items_applied (
  sid TEXT NOT NULL,
  assignment_id TEXT NOT NULL,
  rubric_id TEXT NOT NULL,

  PRIMARY KEY (sid, rubric_id),

  FOREIGN KEY (sid) REFERENCES students (sid)
    ON UPDATE CASCADE
    ON DELETE CASCADE,

  FOREIGN KEY (rubric_id, assignment_id) REFERENCES rubric_items (id, assignment_id)
    ON UPDATE CASCADE
    ON DELETE CASCADE
);
