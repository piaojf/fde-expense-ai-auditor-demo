"""Explicit, idempotent SQLite guard migration; never run by API startup."""

import sqlite3
from pathlib import Path

SQL = Path(__file__).resolve().parents[1] / "database/sqlite/enterprise_travel_guards.sql"


def install(connection):
    connection.executescript(SQL.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path,
                        help="Explicit existing SQLite database; never discovers an active demo")
    target = parser.parse_args().database.resolve(strict=True)
    with sqlite3.connect(target.as_uri() + "?mode=rw", uri=True) as connection:
        install(connection)
    print("Enterprise travel guards installed; existing rows retained")
