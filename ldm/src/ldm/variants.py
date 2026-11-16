"""Python values to and from D-Bus variants (dbus-fast), as the service and its clients exchange them.

The service and the windows share these, so both sides agree on how each Python type is sent.
"""

from __future__ import annotations

from typing import Any

try:
    from dbus_fast import Variant
except Exception:  # noqa: BLE001 (Windows, macOS: no dbus-fast; values stay plain for the local channel's JSON)
    Variant = None


def to_variant(value: Any) -> Variant:
    if Variant is None:
        return value
    if isinstance(value, (bytes, bytearray)):
        return Variant("ay", bytes(value))
    if isinstance(value, bool):
        return Variant("b", value)
    if isinstance(value, int):
        return Variant("x", value)
    if isinstance(value, float):
        return Variant("d", value)
    if isinstance(value, str):
        return Variant("s", value)
    if isinstance(value, dict):
        return Variant("a{ss}", {str(k): str(v) for k, v in value.items()})
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        return Variant("aa{sv}", [to_vardict(v) for v in value])
    if isinstance(value, list) and value and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        return Variant("ax", value)
    if isinstance(value, list):
        return Variant("as", [str(v) for v in value])
    raise TypeError(f"cannot send {type(value).__name__} over D-Bus")


def to_vardict(values: dict[str, Any]) -> dict[str, Variant]:
    """None values are omitted: clients treat a missing key as unknown."""
    return {k: to_variant(v) for k, v in values.items() if v is not None}


def from_vardict(values: dict[str, Variant]) -> dict[str, Any]:
    return {k: v.value for k, v in values.items()}


def unwrap(value: Any) -> Any:
    """A reply or signal body as plain Python: Variants opened, all the way down."""
    if Variant is not None and isinstance(value, Variant):
        return unwrap(value.value)
    if isinstance(value, dict):
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    return value
