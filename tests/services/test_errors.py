"""`DomainError.message`: the text routers send back to the person, the same words the service raised."""

from __future__ import annotations

from krater.services.errors import (
    ClearanceCheckUnavailable,
    DomainError,
    InvalidState,
    NotFound,
    ValidationFailed,
    VerificationRequired,
)
from krater.services.funding_holds import FundingHeld, FundingHold


def test_message_is_what_the_service_said() -> None:
    assert InvalidState("Already submitted.").message == "Already submitted."
    assert str(InvalidState("Already submitted.")) == "Already submitted."


def test_message_defaults_to_empty() -> None:
    assert NotFound().message == ""
    assert DomainError().message == ""


def test_subclasses_with_their_own_init_keep_their_message() -> None:
    failed = ValidationFailed({"title": "Give it a title."})
    assert failed.message == "title: Give it a title."
    assert failed.errors == {"title": "Give it a title."}
    assert ClearanceCheckUnavailable().message.startswith("Krater couldn't reach Weave")
    assert VerificationRequired([("ana", "not verified")]).message.startswith("This project's compute is held")
    assert FundingHeld(next(iter(FundingHold))).message == str(FundingHeld(next(iter(FundingHold))))
