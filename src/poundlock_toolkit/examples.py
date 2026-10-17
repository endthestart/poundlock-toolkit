"""Example configurations shipped inside the package.

A wheel install has no checkout, so the README's first commands read these
through `poundlock-toolkit example NAME --output FILE` instead of a path.
"""

from importlib.resources import files


def _folder():
    return files("poundlock_toolkit") / "data" / "examples"


def names():
    return sorted(
        entry.name.removesuffix(".json")
        for entry in _folder().iterdir()
        if entry.name.endswith(".json")
    )


def text(name):
    if name not in names():
        raise KeyError(name)
    return (_folder() / f"{name}.json").read_text(encoding="utf-8")
