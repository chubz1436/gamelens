from gamelens.session import (
    OPERATOR_MAGIC, publish_operator_token, read_operator_token, clear_operator_token,
)
import pytest


def test_desktop_login_handoff_is_encrypted_and_removed_only_for_owner(tmp_path):
    path = tmp_path / "operator.token"
    publish_operator_token(path, "test-operator-one")
    assert path.read_bytes().startswith(OPERATOR_MAGIC)
    assert b"test-operator-one" not in path.read_bytes()
    assert read_operator_token(path) == "test-operator-one"
    publish_operator_token(path, "test-operator-two")
    clear_operator_token(path, "test-operator-one")
    assert read_operator_token(path) == "test-operator-two"
    clear_operator_token(path, "test-operator-two")
    assert not path.exists()


def test_desktop_login_refuses_plaintext(tmp_path):
    path = tmp_path / "operator.token"
    path.write_text("test-operator")
    with pytest.raises(ValueError, match="Windows-encrypted"):
        read_operator_token(path)
