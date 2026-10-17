"""provider-neutral instance ids

Revision ID: c8f2a4d6e1b3
Revises: b5d1e7a3c920
Create Date: 2026-10-14 22:10:00.000000

`compute_instances.vast_instance_id`, `offer_id` and `machine_id` were Vast's integers. A second provider's ids
aren't (AWS `i-0abc...`, HTCondor `1234.0`), so they become strings named for any provider: `provider_instance_id`,
`provider_offer_id`, `provider_machine_id`. `last_vast_status` becomes `last_provider_status`. The id stays unique
per provider, now under the naming convention's name.

The downgrade turns the ids back into integers, so it refuses while any row holds an id that isn't one.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f2a4d6e1b3"
down_revision: str | Sequence[str] | None = "b5d1e7a3c920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RENAMES = (
    # (old name, new name, nullable)
    ("vast_instance_id", "provider_instance_id", True),
    ("offer_id", "provider_offer_id", False),
    ("machine_id", "provider_machine_id", True),
)


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint("provider_instance", "compute_instances", type_="unique")
    for old, new, nullable in _RENAMES:
        op.alter_column(
            "compute_instances",
            old,
            new_column_name=new,
            type_=sa.String(length=128),
            existing_type=sa.BigInteger(),
            existing_nullable=nullable,
            postgresql_using=f"{old}::text",
        )
    op.alter_column("compute_instances", "last_vast_status", new_column_name="last_provider_status")
    op.create_unique_constraint(
        op.f("uq_compute_instances_provider"), "compute_instances", ["provider", "provider_instance_id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    bad = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM compute_instances WHERE provider_instance_id !~ '^[0-9]{1,18}$' "
                "OR provider_offer_id !~ '^[0-9]{1,18}$' OR provider_machine_id !~ '^[0-9]{1,18}$'"
            )
        )
        .scalar()
    )
    if bad:
        raise RuntimeError(
            f"{bad} compute_instances rows have provider ids that aren't integers (another provider's machines); "
            "the old schema can't hold them. Delete or archive those rows first."
        )
    op.drop_constraint(op.f("uq_compute_instances_provider"), "compute_instances", type_="unique")
    op.alter_column("compute_instances", "last_provider_status", new_column_name="last_vast_status")
    for old, new, nullable in _RENAMES:
        op.alter_column(
            "compute_instances",
            new,
            new_column_name=old,
            type_=sa.BigInteger(),
            existing_type=sa.String(length=128),
            existing_nullable=nullable,
            postgresql_using=f"{new}::bigint",
        )
    op.create_unique_constraint("provider_instance", "compute_instances", ["provider", "vast_instance_id"])
