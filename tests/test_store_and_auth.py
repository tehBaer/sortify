import json

from sortify.spotify import code_challenge, parse_redirect
from sortify.store import Store


def test_store_roundtrip(tmp_path):
    s = Store(tmp_path)
    assert s.config()["client_id"] is None
    s.update_config(client_id="abc", input_ids=["x"])
    s2 = Store(tmp_path)
    assert s2.config()["client_id"] == "abc"
    assert s2.config()["input_ids"] == ["x"]
    # file on disk is plain readable JSON
    raw = json.loads((tmp_path / "config.json").read_text())
    assert raw["client_id"] == "abc"


def test_pkce_challenge_rfc7636_vector():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_parse_redirect():
    code, state = parse_redirect("http://127.0.0.1:8888/callback?code=AQD-xyz&state=s123")
    assert (code, state) == ("AQD-xyz", "s123")
