"""Migrations stay additive, so the previous release still runs on the newer database.

Production ships by moving the `production` branch. When a release fails its health check, or is reverted, the
previous code starts again on the database the newer release migrated (weft skips `alembic upgrade` when the
database is at a revision the code doesn't know). That is safe only when migrations add and never remove. See
"Migrations" in docs/dev/production.md.

This test reads each migration, except its `downgrade()`, and fails on a step that removes, renames, narrows or
rewrites. A backfill (`UPDATE ... SET` of a new column) and dropping a constraint to widen it are fine. Do such a
step in a later release, after no deployed code needs the old shape, and put the line
`# krater: contract-migration (<why it is safe now>)` in that migration's `upgrade()`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"
MARKER = re.compile(r"^\s*# krater: contract-migration \(.+\)\s*$", re.MULTILINE)

REMOVING_OPS = {"drop_table", "drop_column", "rename_table"}
REMOVING_SQL = re.compile(
    r"\bDROP\s+(TABLE|COLUMN|CONSTRAINT|TYPE|SCHEMA|VIEW|FUNCTION)\b"
    r"|\bALTER\s+TABLE\s+\S+\s+DROP\b"
    r"|\bRENAME\s+(TO|COLUMN|CONSTRAINT)\b"
    r"|\bALTER\s+(COLUMN\s+)?\S+\s+(TYPE|SET\s+NOT\s+NULL)\b"
    r"|\bTRUNCATE\b|\bDELETE\s+FROM\b",
    re.IGNORECASE,
)

# From before production existed (2026-11-06). Do not add to this list: use the marker.
BEFORE_PRODUCTION = {
    "4c2a9e7b1d3f_move_roles_into_krater_db.py",
    "67a1656dcb6d_procrastinate_schema.py",
    "a7c1e5d9f2b4_stop_storing_member_data.py",
    "b7ba424bfaeb_weave_owns_roles.py",
    "c8f2a4d6e1b3_provider_neutral_instance_ids.py",
    "e3a9c6b2d714_add_project_members.py",
    "f3b8d4e60a12_drop_skypilot_columns.py",
}


def _without_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.DOTALL)


def _call_steps(call: ast.Call) -> list[str]:
    if not isinstance(call.func, ast.Attribute):
        return []
    name = call.func.attr
    keywords = {k.arg: k.value for k in call.keywords}
    if name in REMOVING_OPS:
        return [f"op.{name}"]
    if name == "alter_column":
        steps = []
        if "new_column_name" in keywords:
            steps.append("alter_column(new_column_name=...)")
        if "type_" in keywords:
            steps.append("alter_column(type_=...)")
        nullable = keywords.get("nullable")
        if isinstance(nullable, ast.Constant) and nullable.value is False:
            steps.append("alter_column(nullable=False)")
        return steps
    if name == "add_column":
        for column in call.args[1:]:
            if not (isinstance(column, ast.Call) and getattr(column.func, "attr", "") == "Column"):
                continue
            kw = {k.arg: k.value for k in column.keywords}
            not_null = isinstance(kw.get("nullable"), ast.Constant) and kw["nullable"].value is False
            if not_null and "server_default" not in kw:
                return ["add_column(nullable=False) without server_default"]
    return []


def _removing_steps(source: str) -> list[str]:
    tree = ast.parse(source)
    downgrade = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == "downgrade"
    ]
    skip = {id(n) for d in downgrade for n in ast.walk(d)}
    steps: list[str] = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Call):
            steps += _call_steps(node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            match = REMOVING_SQL.search(_without_comments(node.value))
            if match:
                steps.append(f"SQL {' '.join(match.group(0).split()).upper()}")
    return steps


def test_upgrades_only_add():
    found = {}
    for path in sorted(VERSIONS.glob("*.py")):
        source = path.read_text()
        if path.name in BEFORE_PRODUCTION or MARKER.search(source):
            continue
        steps = _removing_steps(source)
        if steps:
            found[path.name] = steps
    assert not found, (
        "These migrations remove, rename, narrow or rewrite, so the previous release may not run after them:\n"
        + "\n".join(f"  {name}: {', '.join(steps)}" for name, steps in found.items())
        + "\nAdd the new shape now and remove the old one in a later release, with the line"
        + " `# krater: contract-migration (<why it is safe now>)` in upgrade()."
    )


def test_the_grandfathered_files_exist():
    assert {p.name for p in VERSIONS.glob("*.py")} >= BEFORE_PRODUCTION


def test_the_check_sees_removals():
    source = (
        "def _helper():\n    op.drop_column('projects', 'title')\n\n"
        "def upgrade():\n"
        "    _helper()\n"
        "    q = 'ALTER TABLE projects DROP note'\n"
        "    op.execute(q)\n"
        "    op.execute('DELETE FROM projects')\n"
        "    op.alter_column('projects', 'slug', nullable=False)\n"
        "    op.add_column('projects', sa.Column('kind', sa.Text(), nullable=False))\n"
    )
    assert sorted(_removing_steps(source)) == sorted(
        [
            "op.drop_column",
            "SQL ALTER TABLE PROJECTS DROP",
            "SQL DELETE FROM",
            "alter_column(nullable=False)",
            "add_column(nullable=False) without server_default",
        ]
    )


def test_the_check_allows_additions_and_downgrade():
    source = (
        "def upgrade():\n"
        "    op.add_column('projects', sa.Column('kind', sa.Text(), nullable=False, server_default='a'))\n"
        "    op.create_index('ix_rename_me', 'projects', ['kind'])\n"
        "    op.execute('CREATE TABLE notes (body text) -- DROP TABLE is only a comment')\n\n"
        "def downgrade():\n"
        "    op.drop_column('projects', 'kind')\n"
    )
    assert _removing_steps(source) == []


def test_the_marker_needs_a_reason():
    assert not MARKER.search("# krater: contract-migration\n")
    assert MARKER.search("    # krater: contract-migration (nothing reads old_title since 2026-12-01)\n")
