"""A contact address that already belongs to another lead must not be written.

``email`` and ``phone_e164`` are unique per tenant — that partial index is what
stops one person becoming two leads. The model does not know the constraint
exists, so it saves whatever the prospect types and the violation surfaces at
flush time, far from the tool that caused it.

Found on the live service rather than here. The same address arriving in a
second conversation produced EITHER the agent's "technical problem" apology —
when the autoflush happened inside its try block — OR a bare 500 from the
endpoint when it happened after. One cause, two symptoms that look unrelated,
and both of them a lost conversation.

Not an eval artefact either: one person writing from a second number, or a
colleague giving the shared company address, is an ordinary Tuesday.
"""

import uuid
from typing import Any, Optional

from app.sales.enums import Channel
from app.sales.tools import SalesToolContext, execute_tool
from tests.test_agent import make_lead

TENANT = uuid.uuid4()
SHARED_EMAIL = "mohamed@example.com"
SHARED_PHONE = "+201000000001"


class _Result:
    def __init__(self, hit: Optional[uuid.UUID]) -> None:
        self._hit = hit

    def scalar_one_or_none(self) -> Optional[uuid.UUID]:
        return self._hit


class FakeSession:
    """Answers exactly one question: is this value already taken?

    A stub rather than a real database because nothing else in this suite needs
    one — but it does have to be asked, so it records the calls.
    """

    def __init__(self, taken: bool) -> None:
        self.taken = taken
        self.queries: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.queries.append(statement)
        return _Result(uuid.uuid4() if self.taken else None)


def _lead(**kwargs: Any) -> Any:
    return make_lead(tenant_id=TENANT, **kwargs)


def _ctx(db: Any, lead: Any) -> SalesToolContext:
    return SalesToolContext(
        db=db, tenant_id=str(TENANT), lead=lead, channel=Channel.WHATSAPP
    )


class TestContactUniqueness:
    async def test_taken_email_is_refused_and_left_unwritten(self) -> None:
        lead = _lead()
        db = FakeSession(taken=True)

        result = await execute_tool(
            _ctx(db, lead), "save_lead_details", {"email": SHARED_EMAIL}
        )

        assert lead.email is None, "writing it is what raised IntegrityError"
        assert "email" in result["rejected"]
        assert "email" not in result.get("saved", {})
        assert db.queries, "the database has to actually be asked"

    async def test_taken_phone_is_refused_too(self) -> None:
        lead = _lead()
        db = FakeSession(taken=True)

        result = await execute_tool(
            _ctx(db, lead), "save_lead_details", {"phone": "01000000001"}
        )

        assert lead.phone_e164 is None
        assert "phone" in result["rejected"]

    async def test_a_free_address_is_still_saved(self) -> None:
        lead = _lead()
        db = FakeSession(taken=False)

        result = await execute_tool(
            _ctx(db, lead), "save_lead_details", {"email": "nobody@example.com"}
        )

        assert lead.email == "nobody@example.com"
        assert result["saved"]["email"] == "nobody@example.com"

    async def test_the_refusal_tells_the_model_not_to_keep_asking(self) -> None:
        """A silent drop would have the agent ask for the address again.

        The tool result is the only thing the model sees, so the reason has to
        be in it — and it has to say the record needs a human, because merging
        two leads is not the agent's call.
        """
        db = FakeSession(taken=True)
        result = await execute_tool(
            _ctx(db, _lead()), "save_lead_details", {"email": SHARED_EMAIL}
        )
        reason = result["rejected"]["email"].lower()
        assert "another lead" in reason
        assert "do not ask for it again" in reason

    async def test_a_lead_that_already_has_the_address_is_left_alone(self) -> None:
        """Re-sending its own address must not look like a conflict with itself."""
        lead = _lead(email=SHARED_EMAIL)
        db = FakeSession(taken=True)

        result = await execute_tool(
            _ctx(db, lead), "save_lead_details", {"email": SHARED_EMAIL}
        )

        assert lead.email == SHARED_EMAIL
        # The tool reports no rejections at all, so the key is absent entirely.
        assert "email" not in (result.get("rejected") or {})
        assert not db.queries, "already filled in — no need to ask"
