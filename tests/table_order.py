"""A stable read order for every table a test snapshots, rowid or not (tests only).

The prune proofs compare whole stores table by table. Schema 8 added two
`WITHOUT ROWID` tables (`store_process_claims.sql`), which have no `rowid`
to order by. Their primary key is unique, so it orders every row and is the
order SQLite stores them in. A rowid table keeps the `rowid` order these
snapshots always used.
"""

from __future__ import annotations

import sqlite3


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def stable_order(conn: sqlite3.Connection, table: str) -> str:
    """The `ORDER BY` terms that read `table` in one order: `rowid`, or its primary key."""
    found = conn.execute("SELECT wr FROM pragma_table_list WHERE schema='main' AND name=?",
                         (table,)).fetchone()
    assert found is not None, f"no table {table!r}"
    if not found[0]:
        return "rowid"
    key = sorted((column[5], column[1]) for column in conn.execute(
        f"PRAGMA table_info({_quoted(table)})") if column[5])
    assert key, f"WITHOUT ROWID table {table!r} has no primary key"
    return ", ".join(_quoted(name) for _, name in key)


def ordered_select(conn: sqlite3.Connection, table: str) -> str:
    """`SELECT *` over `table` in its stable order."""
    return f"SELECT * FROM {_quoted(table)} ORDER BY {stable_order(conn, table)}"
