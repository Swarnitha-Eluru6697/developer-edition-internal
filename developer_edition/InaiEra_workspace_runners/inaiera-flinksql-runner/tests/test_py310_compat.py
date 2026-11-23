"""The runner image is Python 3.10 (the tests may run on a newer interpreter). `asyncio.TimeoutError` is only an alias of the builtin
`TimeoutError` from 3.11: `except TimeoutError:` around `asyncio.wait_for` crashed the deployment reconciler loop on the live 3.10 image
(found by the live check of FL-011). Guard the pattern in source so a newer-interpreter test run cannot hide it again."""
from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"


def test_no_bare_timeout_error_handlers_in_the_app():
    offenders = []
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ExceptHandler):
                names = [n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)] if node.type is not None else []
                if "TimeoutError" in names and not any(
                        isinstance(n, ast.Attribute) and n.attr == "TimeoutError" for n in ast.walk(node.type)):
                    offenders.append(f"{path.relative_to(APP.parent)}:{node.lineno}")
    assert offenders == [], f"use asyncio.TimeoutError (Python 3.10 image): {offenders}"
