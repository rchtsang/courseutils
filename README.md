# courseutils

this is my personal library containing various administrative utilities for
the courses i teach.

## rationale

there are basically 3 sources of data i need to be on top of:
- canvas (student enrollment, final grades, communications)
- gradescope (assignment/exam submissions and grading)
- local sqlite database (manual/policy adjustments, data lookup)

having a local database as the source of truth is incredibly convenient for
admin purposes. ideally its state would be mirrored to canvas automatically,
but alas, this is probably more trouble than it's worth.

syncing data between these 3 locations manually is tedious, but can be made
less so with some scripting; this is basically a library containing a bunch
of useful functions to make those scripts/operations easier.

the goal is to keep python as the primary interface for course admin data,
as this makes it much easier to run my own analysis.
no context switching between canvas, gradescope, and internal spreadsheets
when everything is in a single database i can inspect with marimo or jupyter.

## model

- canvas is read/write via `canvasapi`
- gradescope is read-only, data exported as csv
- local database is read/write via `sqlite3`

common functions:
- loading data from gradescope exports to local database
- syncing grades from local database to canvas
- updating student roster from canvas
- applying custom grading rules like clobbers

