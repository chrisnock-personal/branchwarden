import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BRANCHWARDEN_SECRET_KEY", "t" * 64)
os.environ.setdefault("BRANCHWARDEN_KEY_FILE", "/nonexistent/secret.key")

import pytest  # noqa: E402

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture
def db_url(monkeypatch):
    """A fresh, empty Branchwarden database for one test, or a skip when
    TEST_DATABASE_URL isn't set (CI sets it; see .github/workflows/ci.yml)."""
    if not TEST_DATABASE_URL:
        pytest.skip("needs Postgres: set TEST_DATABASE_URL")
    import psycopg2

    import db
    import rules
    monkeypatch.setattr(db, "DATABASE_URL", TEST_DATABASE_URL)
    raw = psycopg2.connect(TEST_DATABASE_URL)
    raw.autocommit = True
    with raw.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    raw.close()
    db.init_schema()
    with db.get_conn() as conn:
        rules.seed(conn)
    return True        # not the URL: it holds a password, and pytest prints fixture values on failure
