"""Migrations stay additive, so the previous release still starts on the newer database.

Production ships by moving the `production` branch, and a bad release is undone by shipping a revert, not by
restoring the database. That works only when a migration adds and never removes: the running version, or the one a
revert brings back, must start on the schema the newer version left. See "Migrations" in docs/dev/production.md.

This test reads the `upgrade()` of each migration and fails on a step that removes or renames something. Drops in
`downgrade()` are fine. Do the removal in a later release, after no deployed code reads the column or table, and
mark that migration with a line `# krater: contract-migration (<why it is safe now>)`.
"""

from __future__ import annotations

import ast
from pathlib import Path

VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"
MARKER = "# krater: contract-migration"

REMOVING_OPS = {"drop_table", "drop_column", "rename_table"}
REMOVING_SQL = ("DROP TABLE", "DROP COLUMN", "RENAME ", "TRUNCATE", "DELETE FROM")

# From before production existed (2026-11-06). Do not add to this list: use the marker.
BEFORE_PRODUCTION = {
    "4c2a9e7b1d3f_move_roles_into_krater_db.py",
    "a7c1e5d9f2b4_stop_storing_member_data.py",
    "b7ba424bfaeb_weave_owns_roles.py",
    "c8f2a4d6e1b3_provider_neutral_instance_ids.py",
    "e3a9c6b2d714_add_project_members.py",
    "f3b8d4e60a12_drop_skypilot_columns.py",
}


def _removing_steps(source: str) -> list[str]:
    steps = []
    for node in ast.parse(source).body:
        if not (isinstance(node, ast.FunctionDef) and node.name == "upgrade"):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            name = call.func.attr
            if name in REMOVING_OPS:
                steps.append(f"op.{name}")
            elif name == "alter_column" and any(k.arg == "new_column_name" for k in call.keywords):
                steps.append("op.alter_column(new_column_name=...)")
            elif name == "execute":
                for const in ast.walk(call):
                    if isinstance(const, ast.Constant) and isinstance(const.value, str):
                        sql = const.value.upper()
                        steps += [f"op.execute({word.strip()} ...)" for word in REMOVING_SQL if word in sql]
    return steps


def test_upgrades_only_add():
    found = {}
    for path in sorted(VERSIONS.glob("*.py")):
        source = path.read_text()
        if path.name in BEFORE_PRODUCTION or MARKER in source:
            continue
        steps = _removing_steps(source)
        if steps:
            found[path.name] = steps
    assert not found, (
        "These migrations remove or rename in upgrade(), so the previous release may not start after them:\n"
        + "\n".join(f"  {name}: {', '.join(steps)}" for name, steps in found.items())
        + f"\nAdd the new thing now and remove the old one in a later release, marked with `{MARKER} (<why>)`."
    )


def test_the_check_sees_a_removal():
    source = 'def upgrade():\n    op.drop_column("projects", "title")\n    op.execute("DELETE FROM projects")\n'
    assert _removing_steps(source) == ["op.drop_column", "op.execute(DELETE FROM ...)"]


def test_the_check_ignores_downgrade():
    source = (
        'def upgrade():\n    op.add_column("projects", None)\n\ndef downgrade():\n    op.drop_column("projects", "x")\n'
    )
    assert _removing_steps(source) == []
