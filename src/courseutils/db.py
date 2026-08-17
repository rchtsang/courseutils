import sqlite3 as sql
from datetime import timedelta
from typing import Iterable, Mapping, Sequence
from pathlib import Path

DB_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

def open_db(path: Path|str) -> sql.Connection:
    """
    Open a database connection.

    Creates a new database at path if necessary.

    :param      path:       path to the sqlite database file
    :returns:   sqlite3.Connection
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(DB_SCHEMA_PATH, 'r') as f:
        schema = f.read()

    db_conn = sql.connect(path)
    db_conn.row_factory = sql.Row
    db_conn.executescript(schema)

    return db_conn


def get_tablenames(db_conn: sql.Connection) -> dict[str,list[str]]:
    """
    Get table names and table column names

    :param      db_conn:    Database connection
    :type       db_conn:    sqlite3.Connection

    :returns:   dictionary mapping table names to column names
    :rtype:     dict[str,list[str]]
    """
    db = db_conn.cursor()

    tblnames = []
    for row in db.execute("SELECT * FROM sqlite_master").fetchall():
        if row['type'] != 'table': continue
        tblnames.append(row['tbl_name'])

    tables = {}
    for tbl_name in tblnames:
        colnames = []
        for row in db.execute(f"PRAGMA table_info('{tbl_name}')").fetchall():
            colnames.append(row['name'])
        tables[tbl_name] = colnames

    return tables


def update_table(
    tbl_name: str,
    db_conn: sql.Connection,
    data: Iterable[Mapping[str,str|int|float|None]],
    keys: Sequence[str],
):
    """
    Updates and/or inserts rows in `tbl_name` from `data`

    Performs (approximately):
    1. UPDATE rows based on matching `keys` columns
    2. INSERT new rows from `data`

    :param      tbl_name:   name of table
    :param      db_conn:    sqlite3.Connection (row_factory as sqlite3.Row)
    :param      data:       iterable of row dictionaries
    :param      keys:       columns that define match scope for updates
    """
    assert isinstance(db_conn, sql.Connection), \
        "not sqlite3.Connection"
    assert len(data := list(data)) > 0, \
        "data empty"
    assert db_conn.row_factory == sql.Row, \
        "sqlite3.Connection row_factory must be sqlite3.Row"
    assert keys, \
        "keys empty"

    tables = get_tablenames(db_conn)

    assert tbl_name in tables, \
        "table not in db: {}".format(tbl_name)

    columns = set(tables[tbl_name])

    for row in data:
        assert set(row) == columns, (
            "row from new data has invalid columns.\n"
            "expected:\n{}\nfound\n{}".format(
                str(list(sorted(set(row)))), str(list(sorted(columns))))
        )

    m = [ k for k in keys if k not in columns ]
    assert len(m) == 0, \
        "invalid keys: {}".format(', '.join(m))

    assert (update_cols := [ col for col in columns if col not in keys ]), \
        "no columns to update"

    columns_list = ', '.join(columns)
    placeholders = ', '.join('?' for _ in columns)

    match_condition = " AND ".join(
        f"target.{col} IS incoming.{col}" for col in keys)

    db = db_conn.cursor()

    db.execute("DROP TABLE IF EXISTS temp._staging")
    db.execute(f"""
        CREATE TEMP TABLE _staging
        AS
        SELECT {columns_list}
        FROM {tbl_name}
        WHERE 0
    """)
    db.executemany(f"""
        INSERT INTO _staging ({columns_list})
        VALUES ({placeholders})
    """, [
        tuple(row[col] for col in columns) for row in data
    ])
    db.execute("""
        UPDATE {tbl_name} AS target
        SET {set_clause}
        FROM _staging AS incoming
        WHERE {match_condition}
    """.format(
        tbl_name=tbl_name,
        set_clause=', '.join(f"{col} = incoming.{col}" for col in update_cols),
        match_condition=match_condition,
    ))
    db.execute("""
        INSERT INTO {tbl_name} ({columns_list})
        SELECT {selected_cols}
        FROM _staging AS incoming
        WHERE NOT EXISTS (
            SELECT 1 FROM {tbl_name} AS target
            WHERE {match_condition}
        )
    """.format(
        tbl_name=tbl_name,
        columns_list=columns_list,
        selected_cols=', '.join(f"incoming.{col}" for col in columns),
        match_condition=match_condition,
    ))
    db.execute("DROP TABLE _staging")



if __name__ == "__main__":
    # standalone testing
    db_conn = sql.connect(":memory:")
    db_conn.row_factory = sql.Row

    db_conn.execute("CREATE TABLE people (first, last, city, status)")
    db_conn.executemany("INSERT INTO people VALUES (?, ?, ?, ?)", [
        ("Ada", "Lovelace", "London", "inactive"),
        ("Grace", "Hopper", "New York", "active"),
        ("Charles", "Babbage", "London", "active"),
        ("George", "Boole", "Lincoln", "inactive"),
    ])
    data = [
        {'first': "Ada", 'last': "Lovelace", 'city': "Oxford", 'status': "active"},
        {'first': "Charles", 'last': "Babbage", 'city': "London", 'status': "inactive"},
        {'first': "Emile", 'last': "Baudot", 'city': "Paris", 'status': "active"},
        {'first': "Claude", 'last': "Shannon", 'city': "Ann Arbor", 'status': "active"},
    ]
    update_table('people', db_conn, data, ['first', 'last'])
    rows = [
        dict(row) for row in db_conn.execute(
            "SELECT * FROM people ORDER BY last, first").fetchall()
    ]
    for row in rows:
        print(row)

