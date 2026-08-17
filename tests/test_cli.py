import sys

import pytest

import sortify.cli as cli
from sortify.cli import DEV_CAP, dev_call_allowed


def test_dev_ceiling_blocks_dev_traffic():
    assert dev_call_allowed(0)
    assert dev_call_allowed(DEV_CAP - 1)
    assert not dev_call_allowed(DEV_CAP)
    assert not dev_call_allowed(DEV_CAP + 500)


# ---- body-argument safety ---------------------------------------------------
#
# The safety-critical property of the body argument is that a malformed or
# unusable body is refused BEFORE Spotify.request is ever reached — that's
# what makes it safe to hand-probe writes at all (see the module docstring
# and CLAUDE.md: "every manual call goes through spx"). These tests pin that
# ordering by making Spotify.request itself the failure: if a future
# refactor moved the parse/validate step below the request call, request
# would be reached and these would fail loudly, rather than a refactor like
# that silently passing CI because no test ever exercised the argument at
# all.


def _forbid_request(monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("spx must refuse a bad body before calling Spotify.request")

    monkeypatch.setattr(cli.Spotify, "request", fail)


def test_malformed_json_body_refuses_before_spending_a_call(monkeypatch, capsys):
    _forbid_request(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["spx", "POST", "/me/playlists", "{not json"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "not valid JSON" in capsys.readouterr().out


@pytest.mark.parametrize("body", ["5", '"x"', "null", "[1, 2]"])
def test_non_object_json_body_refuses_before_spending_a_call(monkeypatch, capsys, body):
    """json.loads happily accepts these — none is a usable request body.

    'null' is the sharpest case: without this guard it would silently
    degrade into sending no body at all, which is not what a caller who
    bothered to pass one asked for.
    """
    _forbid_request(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["spx", "POST", "/me/playlists", body])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "must be a JSON object" in capsys.readouterr().out
