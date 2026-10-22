"""New Work templates: reusable starting instructions loaded from a local JSON file.

A template only fills the assignment field with editable text. It is not sent
to the agent as anything other than that text, and it grants no tools,
permissions or approvals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

TEMPLATES_FILE = Path(__file__).with_name("work_templates.json")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_LIMITS = {"name": 80, "description": 400, "instruction": 4000}


class TemplateError(ValueError):
    pass


def load_templates(path: Path = TEMPLATES_FILE) -> list[dict]:
    """Return the enabled templates in file order. Raises TemplateError for an invalid file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise TemplateError(f"Cannot read {path.name}: {e}") from None
    items = data.get("templates") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise TemplateError(f"{path.name}: 'templates' must be a list.")
    seen, out = set(), []
    for i, t in enumerate(items):
        where = f"{path.name}: template {i + 1}"
        if not isinstance(t, dict):
            raise TemplateError(f"{where} must be an object.")
        if not isinstance(t.get("id"), str) or not _ID.match(t["id"]):
            raise TemplateError(f"{where}: 'id' must be lowercase letters, digits and hyphens.")
        if t["id"] in seen:
            raise TemplateError(f"{where}: duplicate id '{t['id']}'.")
        seen.add(t["id"])
        for field, limit in _LIMITS.items():
            if not isinstance(t.get(field), str) or len(t[field]) > limit:
                raise TemplateError(f"{where}: '{field}' must be text of at most {limit} characters.")
        if not t["name"].strip():
            raise TemplateError(f"{where}: 'name' must not be empty.")
        if not isinstance(t.get("enabled"), bool):
            raise TemplateError(f"{where}: 'enabled' must be true or false.")
        if t["enabled"]:
            out.append({k: t[k] for k in ("id", "name", "description", "instruction")})
    if not out:
        raise TemplateError(f"{path.name}: at least one template must be enabled.")
    return out


__all__ = ["TEMPLATES_FILE", "TemplateError", "load_templates"]
