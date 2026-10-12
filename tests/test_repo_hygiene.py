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
        ("profile.local.yaml", True),
        ("profile.example.yaml", False),
        ("tests/fixtures/profile.yaml", False),
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


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_real_reviews_stay_out_of_the_repository() -> None:
    """Every review of the local profile (if there is one) appears in no file Git
    would take: tracked files and new files that are not ignored."""
    local = ROOT / "profile.local.yaml"
    if not local.is_file():
        pytest.skip("no profile.local.yaml here")
    from app.drafts.profile import load_profile

    reviews = [r.text for r in load_profile(local).reviews]
    # A distinctive piece of each review is enough to catch a copy.
    needles = [" ".join(text.split()[:6]) for text in reviews if len(text.split()) >= 6]
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("not a git checkout")
    leaks = []
    for name in filter(None, proc.stdout.decode().split("\0")):
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            content = " ".join(path.read_text(encoding="utf-8").split())
        except UnicodeDecodeError:
            continue
        leaks += [name for needle in needles if needle in content]
    assert leaks == []
