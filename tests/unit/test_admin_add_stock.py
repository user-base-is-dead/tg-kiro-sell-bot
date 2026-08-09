from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app.services.catalog_service import add_stock


def test_handler_calls_add_stock_with_a_valid_signature() -> None:
    """products.py called add_stock positionally against a keyword-only signature, so every
    "Add Stock" tap raised TypeError and no product could ever be stocked. Binding the call the
    handler makes proves the arguments actually fit."""
    sig = inspect.signature(add_stock)

    # The exact call the handler makes, minus the session.
    sig.bind(
        None,
        product_id=1,
        plaintext_payloads=["KEY-1"],
        added_by_admin_id=42,
    )


def test_add_stock_rejects_positional_arguments() -> None:
    """Locks in why the bug happened, so nobody "fixes" it by loosening the signature."""
    sig = inspect.signature(add_stock)
    with pytest.raises(TypeError):
        sig.bind(None, 1, ["KEY-1"], 42)


def test_no_add_stock_call_passes_extra_positional_args() -> None:
    """A static scan, because the real call sits behind an FSM state and a Telegram Document —
    expensive to reach in a unit test, and this is the failure that actually shipped."""
    app_dir = pathlib.Path(__file__).resolve().parents[2] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", "")) == "add_stock"
                and len(node.args) > 1
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        f"add_stock is keyword-only after `session`; positional call(s) at {offenders} "
        f"raise TypeError at runtime."
    )
