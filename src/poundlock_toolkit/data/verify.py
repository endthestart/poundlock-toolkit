"""Protected independent verifier. Run only inside a networkless sandbox.

Behavioral cases adapted from the author's earlier slug-normalization fixture.
"""

import ast
import importlib.util
import sys

# -I ignores PYTHONDONTWRITEBYTECODE; bytecode in /work would be an unauthorized file.
sys.dont_write_bytecode = True


def verify(path):
    # A deliberately narrow source grammar keeps the candidate from monkeypatching
    # the verifier or manufacturing a success exit. This is a micro-fixture, not
    # an arbitrary-Python security boundary; the container remains mandatory.
    source = open(path, encoding="utf-8").read(32769)
    if len(source) > 32768:
        return False
    tree = ast.parse(source)
    allowed = (
        ast.Module,
        ast.Import,
        ast.alias,
        ast.FunctionDef,
        ast.arguments,
        ast.arg,
        ast.Return,
        ast.If,
        ast.Raise,
        ast.Assign,
        ast.Expr,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Call,
        ast.Attribute,
        ast.UnaryOp,
        ast.Not,
        ast.Compare,
        ast.Is,
        ast.IsNot,
        ast.Eq,
        ast.NotEq,
        ast.keyword,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            return False
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return False
        if isinstance(node, ast.Attribute) and node.attr not in (
            "normalize",
            "encode",
            "decode",
            "lower",
            "strip",
            "sub",
        ):
            return False
        if isinstance(node, ast.Import) and any(
            a.name not in ("re", "unicodedata") or a.asname for a in node.names
        ):
            return False
        if isinstance(node, ast.FunctionDef) and (
            node.name != "slugify" or node.decorator_list
        ):
            return False
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id not in ("isinstance", "TypeError", "str")
        ):
            return False
    if sum(isinstance(n, ast.FunctionDef) for n in tree.body) != 1:
        return False
    if any(not isinstance(n, (ast.Import, ast.FunctionDef)) for n in tree.body):
        return False
    spec = importlib.util.spec_from_file_location("candidate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = [
        ("Hello, World!", "hello-world"),
        ("  Multiple   separators___here ", "multiple-separators-here"),
        ("Café déjà vu", "cafe-deja-vu"),
        ("release-2026.09", "release-2026-09"),
        ("---", ""),
        ("", ""),
    ]
    for text, expected in cases:
        actual = module.slugify(text)
        if type(actual) is not str or actual != expected:
            return False
    try:
        module.slugify(123)
    except TypeError:
        return True
    return False


if __name__ == "__main__":
    try:
        passed = verify(sys.argv[1])
    except BaseException:
        passed = False
    print("7 checks passed" if passed else "verification failed")
    raise SystemExit(0 if passed else 1)
