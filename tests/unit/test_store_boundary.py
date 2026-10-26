"""Only the store module turns store paths into files.

Callers hold references and go through the evidence store. This check reads
the source of every other module and fails on the two ways a caller has built
a store path itself: reading ``<config>.store.path``, and joining a path with
a ``pdf_path`` (``root / pdf_path``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "receipt_index"
_ALLOWED = {"store.py"}


def _names_pdf_path(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "pdf_path") or (
        isinstance(node, ast.Attribute) and node.attr == "pdf_path"
    )


def violations(source: str) -> list[str]:
    """Each place in ``source`` that builds a store path itself."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "path"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "store"
        ):
            found.append(f"line {node.lineno}: reads .store.path")
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and (_names_pdf_path(node.left) or _names_pdf_path(node.right))
        ):
            found.append(f"line {node.lineno}: joins a path with pdf_path")
    return found


def _modules() -> list[Path]:
    return sorted(p for p in _PACKAGE.rglob("*.py") if p.name not in _ALLOWED)


@pytest.mark.parametrize("module", _modules(), ids=lambda p: p.name)
def test_no_module_outside_the_store_builds_a_store_path(module: Path) -> None:
    assert violations(module.read_text()) == []


@pytest.mark.parametrize(
    "source",
    [
        "store = LocalFileStore(Path(config.store.path))",
        "candidate = (root / pdf_path).resolve()",
        "path = root / receipt.pdf_path",
    ],
)
def test_the_check_catches_each_pattern(source: str) -> None:
    assert violations(source) != []
