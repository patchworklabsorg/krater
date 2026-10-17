"""ProjectSecret: a value a project's jobs need (a Hugging Face token, say), stored encrypted.

Only the ciphertext is stored, encrypted with `KRATER_PROJECT_SECRETS_KEY_FILE` (`krater.services.project_secrets`),
which lives apart from the database and `KRATER_SECRET_KEY`, so a database dump alone leaks nothing. The runner
fetches a project's secrets when it starts; they never go into Vast's create request.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from krater.db import Base
from krater.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ProjectSecret(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "project_secrets"
    __table_args__ = (sa.UniqueConstraint("project_id", "name", name="project_secret_name"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The environment variable the job sees, such as HF_TOKEN.
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # Fernet token (base64 text). Never decrypted outside `krater.services.project_secrets`.
    ciphertext: Mapped[str] = mapped_column(sa.Text, nullable=False)
    updated_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False)
