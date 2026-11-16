"""
The database adapter (app/database.py): the same SQL runs on SQLite and on
PostgreSQL.

The translation checks need no database. With KQ_TEST_POSTGRES_URL set to a
disposable PostgreSQL database, the round-trip checks also run against it:
they create the schema there and drop every table afterwards, so never point
it at a database whose data matters.

    python tools/test_database.py
    KQ_TEST_POSTGRES_URL=postgresql://user:pass@127.0.0.1:5432/scratch python tools/test_database.py
"""
import os
import sqlite3
import sys
import tempfile
import traceback
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

config.APP_DB_PATH = Path(tempfile.mkdtemp()) / "adapter.db"
config.DATABASE_URL = ""

from app import database  # noqa: E402

PG_URL = os.getenv("KQ_TEST_POSTGRES_URL", "").strip()


def test_placeholders_and_percent_signs_are_translated():
    sql, returns_id = database._pg_sql(
        "UPDATE transactions SET status = 'simulated' WHERE status LIKE 'simulated%' AND id = ?", True)
    assert sql == "UPDATE transactions SET status = 'simulated' WHERE status LIKE 'simulated%%' AND id = %s", sql
    assert not returns_id
    # Without parameters psycopg does not read % at all, so it is left alone.
    assert database._pg_sql("SELECT 'a%' AS x", False)[0] == "SELECT 'a%' AS x"


def test_inserts_into_numbered_tables_return_the_new_id():
    sql, returns_id = database._pg_sql("INSERT INTO handovers (session_id) VALUES (?)", True)
    assert returns_id and sql.endswith(" RETURNING id"), sql
    sql, returns_id = database._pg_sql("INSERT INTO chat_sessions (session_id) VALUES (?)", True)
    assert not returns_id and "RETURNING" not in sql, "chat_sessions has no id column"
    sql, _ = database._pg_sql("INSERT INTO handovers (session_id) VALUES (?) RETURNING id", True)
    assert sql.count("RETURNING") == 1


def test_case_insensitive_usernames():
    sql, _ = database._pg_sql("SELECT * FROM staff_users WHERE username = ? COLLATE NOCASE", True)
    assert sql == "SELECT * FROM staff_users WHERE lower(username) = lower(%s)", sql


def test_schema_is_translated():
    schema = database._pg_schema()
    assert "AUTOINCREMENT" not in schema and "COLLATE" not in schema
    assert "BIGSERIAL PRIMARY KEY" in schema
    assert " REAL" not in schema.replace("DOUBLE PRECISION", ""), "REAL is single precision in PostgreSQL"
    assert "lower(username)" in schema


def test_rows_read_by_name_and_position():
    row = database.Row({"n": 3, "label": "x"})
    assert row["n"] == 3 and row[0] == 3 and row[1] == "x" and dict(row) == {"n": 3, "label": "x"}
    assert database._plain(Decimal("0.5")) == 0.5 and isinstance(database._plain(Decimal("0.5")), float)


def test_booleans_are_stored_as_integers():
    seen = {}

    class FakeCursor:
        description = None
        rowcount = 1

        def execute(self, sql, params):
            seen["params"] = params

    class FakeConn:
        def cursor(self, row_factory=None):
            return FakeCursor()

    database._PgConnection(FakeConn()).execute("INSERT INTO booking_lookups_x VALUES (?, ?)", ("s", True))
    assert seen["params"] == ("s", 1), seen


def test_sqlite_stays_the_default():
    assert not database.using_postgres()
    database.init_db()
    with database._connect() as conn:
        assert isinstance(conn, sqlite3.Connection)
    assert sqlite3.IntegrityError in database.IntegrityError


def test_health_says_which_database_without_its_address():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    assert client.get("/api/health").json()["database"] == "sqlite"
    config.DATABASE_URL = "postgresql://user:secret@db.example:5432/x"
    try:
        body = client.get("/api/health").text
        assert '"database":"postgresql"' in body and "secret" not in body and "db.example" not in body, body
    finally:
        config.DATABASE_URL = ""


def test_round_trip_on_postgresql():
    if not PG_URL:
        print("        (skipped: KQ_TEST_POSTGRES_URL not set)")
        return
    config.DATABASE_URL = PG_URL
    try:
        with database._connect() as conn:                    # start from an empty database
            conn.executescript("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        database.init_db()
        database.init_db()                                   # a second start changes nothing
        first = database.log_message("pg_s1", "user", "hello")
        second = database.log_message("pg_s1", "assistant", "hi")
        assert isinstance(first, int) and second == first + 1
        assert [m["message"] for m in database.fetch_conversation("pg_s1")] == ["hello", "hi"]
        database.create_chat_session("pg_s1", "127.0.0.1")
        database.create_chat_session("pg_s1", "127.0.0.2")  # replaces, as INSERT OR REPLACE did
        assert database.get_chat_session("pg_s1")["ip"] == "127.0.0.2"
        with database._connect() as conn:
            conn.execute("INSERT INTO staff_users (username, role, password_hash, created_at) "
                         "VALUES (?, ?, ?, ?)", ("Wambui", "agent", "x", "2026-11-16"))
            row = conn.execute("SELECT * FROM staff_users WHERE username = ? COLLATE NOCASE",
                               ("wambui",)).fetchone()
            assert row and row["username"] == "Wambui"
            assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 2
        try:
            with database._connect() as conn:
                conn.execute("INSERT INTO staff_users (username, role, password_hash, created_at) "
                             "VALUES (?, ?, ?, ?)", ("WAMBUI", "agent", "x", "2026-11-16"))
            raise AssertionError("a username differing only in case was accepted")
        except database.IntegrityError:
            pass
        with database._connect() as conn:                    # the failed insert was rolled back
            assert conn.execute("SELECT COUNT(*) FROM staff_users").fetchone()[0] == 1
    finally:
        with database._connect() as conn:
            conn.executescript("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        database.close_pool()
        config.DATABASE_URL = ""


def main() -> int:
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception:
            failures += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
