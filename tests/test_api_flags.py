"""API booleans: only JSON true and false count; a string like "false" is refused, not taken as true."""

import pytest

import api


def test_flag_takes_json_booleans_only():
    assert api.flag({"x": True}, "x") is True and api.flag({"x": False}, "x") is False
    assert api.flag({}, "x") is False and api.flag({}, "x", None) is None
    for bad in ("false", "true", 0, 1, "no"):
        with pytest.raises(api._Bad):
            api.flag({"x": bad}, "x")
