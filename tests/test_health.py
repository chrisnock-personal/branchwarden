import os
import time
from datetime import datetime, timedelta, timezone

import data
import db
import healthcheck

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
M = lambda n: NOW - timedelta(minutes=n)   # noqa: E731


def test_scans_keeping_up():
    assert data.scan_overdue(M(15), None, M(600), 10, NOW) is None


def test_overdue_after_twice_the_interval():
    assert data.scan_overdue(M(21), None, M(600), 10, NOW) == {"since": M(21), "interval": 10}


def test_a_long_scan_in_progress_is_not_a_fault_but_a_stuck_one_is():
    assert data.scan_overdue(M(45), M(5), M(600), 10, NOW) is None
    assert data.scan_overdue(M(45), M(30), M(600), 10, NOW) is not None


def test_new_install_counts_from_the_first_account():
    assert data.scan_overdue(None, None, None, 10, NOW) is None          # nobody set up yet
    assert data.scan_overdue(None, None, M(5), 10, NOW) is None
    assert data.scan_overdue(None, None, M(25), 10, NOW) == {"since": None, "interval": 10}


def test_worker_heartbeat(tmp_path, monkeypatch):
    path = tmp_path / "alive"
    monkeypatch.setattr(healthcheck, "HEARTBEAT", str(path))
    assert not healthcheck.worker_ok()                 # never beat
    healthcheck.beat()
    assert healthcheck.worker_ok()
    old = time.time() - healthcheck.STALE_SECONDS - 5
    os.utime(path, (old, old))
    assert not healthcheck.worker_ok()
    healthcheck.beat()                                  # beating again refreshes it
    assert healthcheck.worker_ok()


def _log(conn, finished, *, started=None, trigger="schedule"):
    db.execute(conn, """INSERT INTO scan_log (trigger, started_at, finished_at) VALUES (%s, %s, %s)""",
               (trigger, started or finished - timedelta(minutes=1), finished))


def _user(conn):
    db.execute(conn, """INSERT INTO users (email, name, role, created_at)
                        VALUES ('a@example.com', 'A', 'admin', %s)""", (M(600),))


def test_worker_overdue_reads_the_last_full_scan(db_url):
    """The banner's database half: only finished full scans count, and the
    interval comes from settings (twice it, not a fixed number of minutes)."""
    with db.get_conn() as conn:
        _user(conn)
        _log(conn, M(15))
        assert data.worker_overdue(conn, now=NOW) is None

        db.execute(conn, "DELETE FROM scan_log")
        _log(conn, M(45), trigger="webhook")             # partial scans don't keep the banner away
        _log(conn, M(15))                                # but a recent full one does
        assert data.worker_overdue(conn, now=NOW) is None

        db.execute(conn, "DELETE FROM scan_log")
        _log(conn, M(45), trigger="webhook")
        _log(conn, M(21))                                # last full scan was 21 min ago
        assert data.worker_overdue(conn, now=NOW) == {"since": M(21), "interval": 10}

        db.execute(conn, """INSERT INTO settings (key, value) VALUES ('SCAN_INTERVAL_MINUTES', '30')
                            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""")
        assert data.worker_overdue(conn, now=NOW) is None   # 21 min is fine on a 30 min interval
        db.execute(conn, "DELETE FROM scan_log")
        _log(conn, M(65))
        assert data.worker_overdue(conn, now=NOW) == {"since": M(65), "interval": 30}


def test_worker_overdue_ignores_a_scan_still_in_progress(db_url):
    with db.get_conn() as conn:
        _user(conn)
        _log(conn, None, started=M(5))                   # long scan, still running: not a fault
        assert data.worker_overdue(conn, now=NOW) is None
        db.execute(conn, "DELETE FROM scan_log")
        _log(conn, M(45), started=M(30))                 # last finished long ago; this one is stuck
        assert data.worker_overdue(conn, now=NOW) is not None
