"""requirements.txt mirrors the dependencies declared in pyproject.toml."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _requirements() -> list[str]:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def test_requirements_txt_lists_exactly_the_declared_dependencies():
    with (ROOT / "pyproject.toml").open("rb") as f:
        project = tomllib.load(f)["project"]
    declared = [*project["dependencies"], *project["optional-dependencies"]["build"],
                *project["optional-dependencies"]["dev"]]

    assert sorted(_requirements(), key=str.lower) == sorted(declared, key=str.lower)


def test_requirements_have_lower_bounds():
    assert all(">=" in line for line in _requirements())
