"""Session authority is explicitly scoped to one named client."""
import json

from test_multi_client import pair, Stdio


def test_session_control_isolated_between_two_stdio_clients(pair):
    a, b, config = pair
    c = Stdio(config)
    try:
        for arguments in ({"action": "arm"}, {"client": "missing", "action": "live"}):
            assert c.tool("gamelens_session", arguments)["isError"]
        assert a.requests == b.requests == []
        for action in ("arm", "live"):
            result = c.tool("gamelens_session", {"client": "alpha", "action": action})
            assert not result["isError"]
            assert json.loads(result["content"][0]["text"]) == {"client": "alpha"}
        assert a.safety == {"armed": True, "dry_run": False, "killed": False}
        assert b.safety == {"armed": False, "dry_run": True, "killed": False}
        assert b.requests == []
        assert [(m, path, token) for m, path, _, token in a.requests] == [
            ("POST", "/arm", "alpha-token"), ("POST", "/live", "alpha-token")]
        assert not c.tool("gamelens_session", {"client": "alpha", "action": "stop"})["isError"]
        assert a.safety["killed"] and not b.safety["killed"]
        assert not a.acts() and not b.acts()
    finally:
        c.close()
