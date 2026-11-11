import os
import re
import tomllib
from pathlib import Path

from pi_factory.config import parse_config
from pi_factory.secrets import load_secrets

DOC = Path(__file__).parent.parent / "docs" / "configuration.md"


def block(lang: str) -> str:
    text = DOC.read_text()
    m = re.search(rf"```{lang}\n(.*?)```", text, re.S)
    assert m, f"no {lang} block in docs/configuration.md"
    return m.group(1)


def test_documented_factory_toml_validates():
    cfg = parse_config(tomllib.loads(block("toml")))
    assert cfg.github.owner == "olivia"
    assert cfg.repos["pi-relay"].repo == "acme/pi-relay"
    assert cfg.repos["pi-relay"].url == "https://github.com/acme/pi-relay.git"
    assert cfg.repos["pi-relay"].checks[0] == "pnpm typecheck"
    assert cfg.poll_interval_seconds == 120 and cfg.max_concurrent == 2


def test_documented_secrets_env_loads(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text(block("env"))
    os.chmod(p, 0o600)
    s = load_secrets(p)
    assert s.github_token() == "ghp_xxx"
    assert s.has("PI_FACTORY_GITHUB_TOKEN")  # the one and only secret
