import os
import time
from datetime import datetime, timedelta, timezone

import data
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
