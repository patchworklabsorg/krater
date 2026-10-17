"""UserSshKey: a member's SSH public key, for their Krater Compute boxes.

Only the key itself is stored (`key_type` and `key_base64`), never options or a comment that could smuggle a second
line or `command=` into a machine's authorized_keys. Krater writes a member's keys only into their own boxes, through
the runner; it never adds them to the organization's Vast account.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base
from krater.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin


class UserSshKey(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "user_ssh_keys"
    __table_args__ = (sa.UniqueConstraint("user_id", "fingerprint", name="user_fingerprint"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    key_type: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    key_base64: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # SHA256:<base64>, as `ssh-keygen -lf` prints it.
    fingerprint: Mapped[str] = mapped_column(sa.String(100), nullable=False)

    @property
    def authorized_keys_line(self) -> str:
        return f"{self.key_type} {self.key_base64}"
