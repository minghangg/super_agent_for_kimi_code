"""Per-Issue AT/standard flow selection."""
from __future__ import annotations

AT = "at"
STANDARD = "standard"
LABELS = {AT: "flow:at", STANDARD: "flow:standard"}


class FlowError(ValueError):
    pass


def from_labels(labels) -> str:
    names = set()
    for item in labels or []:
        if isinstance(item, dict):
            names.add(str(item.get("name") or "").strip().lower())
        else:
            names.add(str(item or "").strip().lower())
    selected = [name for name, label in LABELS.items() if label in names]
    if len(selected) > 1:
        raise FlowError("Issue cannot have both flow:at and flow:standard")
    return selected[0] if selected else ""


def resolve(labels, *, default_at=False) -> str:
    selected = from_labels(labels)
    if selected:
        return selected
    return AT if default_at else STANDARD


def from_row(row: dict, *, default_at=False) -> str:
    return resolve((row or {}).get("labels") or [], default_at=default_at)
