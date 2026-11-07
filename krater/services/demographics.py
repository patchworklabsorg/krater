"""The demographics gate: a member answers Weave's demographics form before they submit a grant application.

Funders need these answers. Weave keeps them sealed and never sends them to Krater. Weave's directory record only
says `demographics_answered: true` once the member has saved an answer. Krater stores none of this.

- **Who.** The member who submits: the person who presses "Submit for review" on a proposal or an amendment. A
  completion asks for no budget, so it isn't gated. Saving a draft is never gated.
- **The rule.** Submission is refused unless Weave says `demographics_answered: true`. The directory is asked
  fresh at decision time.
- **No directory record.** A 404 refuses the submission, the same as the verification check
  (`krater.services.clearance`): Weave hides anyone Krater may not serve.
- **Field missing.** Older Weave versions send a record without `demographics_answered`. Krater follows the
  verification check's choice for a missing `minor`: it doesn't block, and it logs a warning, so submissions keep
  working while the Weave rollout finishes.
- **Weave down.** The submission fails (`DemographicsCheckUnavailable`). Krater never guesses.
"""

from __future__ import annotations

import logging

from krater.models import User
from krater.services.errors import DemographicsCheckUnavailable, DemographicsRequired
from krater.weave import WeaveClient, WeaveUnavailableError

logger = logging.getLogger(__name__)


def require_answered(weave_client: WeaveClient, user: User) -> None:
    """The gate before a grant application is submitted. Raises `DemographicsRequired` when Weave says `user` hasn't
    answered the demographics form or doesn't return them (404), and `DemographicsCheckUnavailable` when Weave can't
    be reached. Both are `InvalidState`."""
    try:
        record = weave_client.get_user(user.weave_sub, fresh=True)
    except WeaveUnavailableError as exc:
        raise DemographicsCheckUnavailable() from exc
    if record is None:
        raise DemographicsRequired()
    if record.demographics_answered is None:
        # The same choice as a missing `minor` in `krater.services.guardian`: an older Weave doesn't send the
        # field yet, so don't block every submission during the rollout. The warning shows when it is missing.
        logger.warning(
            "Weave didn't say whether member %s answered the demographics form (no demographics_answered field); "
            "letting the submission through",
            user.weave_sub,
        )
        return
    if record.demographics_answered is not True:
        raise DemographicsRequired()


__all__ = ["require_answered"]
