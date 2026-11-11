import os

import pytest

from pi_factory import secrets as sec


@pytest.fixture(autouse=True)
def _clean():
    sec.forget_all_secrets()
    yield
    sec.forget_all_secrets()


def write(tmp_path, text, mode):
    p = tmp_path / "secrets.env"
    p.write_text(text)
    os.chmod(p, mode)
    return p


def test_open_permissions_refused(tmp_path):
    p = write(tmp_path, "PI_FACTORY_GITHUB_TOKEN=abc123456\n", 0o644)
    with pytest.raises(sec.SecretsError) as exc:
        sec.load_secrets(p)
    assert str(p) in str(exc.value) and "0600" in str(exc.value)
    assert "abc123456" not in str(exc.value)


def test_group_readable_refused(tmp_path):
    p = write(tmp_path, "A=b\n", 0o640)
    with pytest.raises(sec.SecretsError):
        sec.load_secrets(p)


def test_loads_and_exposes_via_accessors(tmp_path):
    p = write(
        tmp_path,
        "# comment\nPI_FACTORY_LINEAR_CLIENT_ID=cid-12345\n"
        'PI_FACTORY_LINEAR_CLIENT_SECRET="csecret-1"\nexport PI_FACTORY_GITHUB_TOKEN=ghp_token99\n',
        0o600,
    )
    s = sec.load_secrets(p)
    assert s.linear_client_id() == "cid-12345"
    assert s.linear_client_secret() == "csecret-1"
    assert s.github_token() == "ghp_token99"
    assert "ghp_token99" not in repr(s)
    with pytest.raises(sec.SecretsError, match="PI_FACTORY_LINEAR_API_KEY"):
        s.linear_api_key()


def test_missing_file(tmp_path):
    with pytest.raises(sec.SecretsError, match="cannot read"):
        sec.load_secrets(tmp_path / "none")


def test_secret_value_redacted_from_raised_error(tmp_path):
    p = write(tmp_path, "PI_FACTORY_GITHUB_TOKEN=ghp_supersecret\n", 0o600)
    sec.load_secrets(p)

    class BoomError(sec.RedactedError):
        pass

    with pytest.raises(BoomError) as exc:
        raise BoomError("401 for Authorization: Bearer ghp_supersecret")
    assert "ghp_supersecret" not in str(exc.value)
    assert "[redacted]" in str(exc.value)
    assert "ghp_supersecret" not in sec.redact("token=ghp_supersecret!")
