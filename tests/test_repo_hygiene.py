import re
import shutil
import subprocess

import pytest

from app.config import Settings
from tests.conftest import ROOT


def test_env_example_has_keys_only() -> None:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    entries = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    for line in entries:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*=", line), (
            f"value or junk in .env.example: {line.split('=')[0]}"
        )
    keys = {line[:-1] for line in entries}
    assert set(Settings.model_fields) | {"TEST_DATABASE_URL"} <= keys


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.parametrize(
    ("path", "ignored"),
    [
        (".env", True),
        (".env.local", True),
        ("main.session", True),
        ("main.session-journal", True),
        (".venv/x", True),
        (".env.example", False),
        ("app/config.py", False),
    ],
)
def test_gitignore(path: str, ignored: bool) -> None:
    proc = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if proc.returncode == 128:
        pytest.skip("not a git checkout")
    assert (proc.returncode == 0) is ignored
