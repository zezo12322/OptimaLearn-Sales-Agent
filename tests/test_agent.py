"""The agent loop and its tools, with the model stubbed out.

What matters here is not the wording the model produces — it is that the loop
always terminates with something sendable, that a crash becomes a handoff rather
than silence, and that ``save_lead_details`` cannot quietly rewrite a lead's
identity.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import pytest

from app.models.sales import Lead
from app.sales import agent as agent_module
from app.sales import tools as tools_module
from app.sales.enums import Channel, Segment, Stage
from app.sales.tools import TOOL_NAMES, TOOL_SCHEMAS, SalesToolContext, execute_tool


# ----------------------------------------------------------------------
# Stubs
# ----------------------------------------------------------------------
@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction


class FakeMessage:
    def __init__(
        self, content: Optional[str] = None, tool_calls: Optional[list] = None
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeResponse:
    def __init__(self, message: FakeMessage) -> None:
        self.choices = [type("Choice", (), {"message": message})()]


class FakeCompletions:
    """Returns scripted replies and records what it was asked."""

    def __init__(self, script: list[FakeMessage], raises: Optional[Exception] = None):
        self.script = list(script)
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        # When the loop withholds tools, answer with text so it can terminate.
        if not kwargs.get("tools"):
            for message in self.script:
                if message.tool_calls is None:
                    return FakeResponse(message)
            return FakeResponse(FakeMessage(content="Final answer."))
        return FakeResponse(self.script.pop(0) if self.script else FakeMessage("ok"))


class FakeClient:
    def __init__(self, completions: FakeCompletions) -> None:
        self.chat = type("Chat", (), {"completions": completions})()


def make_lead(**overrides: Any) -> Lead:
    """An unattached ORM instance — column defaults only apply on flush."""
    defaults = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "segment": Segment.UNKNOWN.value,
        "stage": Stage.NEW.value,
        "score": 0,
        "qualification": {},
        "human_takeover": False,
        "marketing_opt_in": False,
        "locale": "ar",
    }
    defaults.update(overrides)
    return Lead(**defaults)


def install(monkeypatch: pytest.MonkeyPatch, completions: FakeCompletions) -> None:
    monkeypatch.setattr(agent_module, "client", FakeClient(completions))


# ----------------------------------------------------------------------
# Tool schemas
# ----------------------------------------------------------------------
class TestToolSchemas:
    def test_every_schema_has_an_executor(self) -> None:
        """A tool the model can call but we cannot run is a guaranteed dead end."""
        assert set(tools_module._EXECUTORS) == TOOL_NAMES

    def test_schemas_are_well_formed(self) -> None:
        for schema in TOOL_SCHEMAS:
            assert schema["type"] == "function"
            function = schema["function"]
            assert function["name"]
            assert function["description"]
            params = function["parameters"]
            assert params["type"] == "object"
            assert isinstance(params["properties"], dict)
            for name, spec in params["properties"].items():
                assert "type" in spec, f"{function['name']}.{name} has no type"

    def test_required_fields_exist_in_properties(self) -> None:
        for schema in TOOL_SCHEMAS:
            function = schema["function"]
            params = function["parameters"]
            for required in params.get("required", []):
                assert required in params["properties"]

    def test_save_lead_details_cannot_set_stage_or_score(self) -> None:
        """Both are derived; letting the model set them would make them meaningless."""
        save = next(
            s["function"]
            for s in TOOL_SCHEMAS
            if s["function"]["name"] == "save_lead_details"
        )
        properties = save["parameters"]["properties"]
        assert "stage" not in properties
        assert "score" not in properties


# ----------------------------------------------------------------------
# save_lead_details
# ----------------------------------------------------------------------
class TestSaveLeadDetails:
    async def test_captures_and_normalises(self) -> None:
        lead = make_lead()
        ctx = SalesToolContext(
            db=None, tenant_id=str(lead.tenant_id), lead=lead, channel=Channel.WHATSAPP
        )
        result = await execute_tool(
            ctx,
            "save_lead_details",
            {
                "full_name": "  زياد محمد  ",
                "phone": "0100 123 4567",
                "email": "Zeyad@Example.com",
                "segment": "B2B",
                "need": "تدريب 30 موظف",
                "seats": "about 30",
                "timeline": "immediate",
                "authority": "decision_maker",
            },
        )
        assert lead.full_name == "زياد محمد"
        assert lead.phone_e164 == "+201001234567"
        assert lead.email == "zeyad@example.com"
        assert lead.segment == "B2B"
        assert lead.qualification["seats"] == 30
        assert lead.qualification["timeline"] == "IMMEDIATE"
        assert lead.qualification["authority"] == "DECISION_MAKER"
        assert result["saved"]

    async def test_never_overwrites_a_known_contact_detail(self) -> None:
        """Corrections are a human's job; a model rewriting identity is a data loss bug."""
        lead = make_lead(email="real@example.com", phone_e164="+201000000000")
        ctx = SalesToolContext(
            db=None, tenant_id=str(lead.tenant_id), lead=lead, channel=Channel.WHATSAPP
        )
        await execute_tool(
            ctx,
            "save_lead_details",
            {"email": "typo@example.com", "phone": "01111111111"},
        )
        assert lead.email == "real@example.com"
        assert lead.phone_e164 == "+201000000000"

    async def test_segment_may_be_corrected(self) -> None:
        """Asking for yourself and then for your team is a real re-segmentation."""
        lead = make_lead(segment=Segment.B2C.value)
        ctx = SalesToolContext(
            db=None, tenant_id=str(lead.tenant_id), lead=lead, channel=Channel.WHATSAPP
        )
        await execute_tool(ctx, "save_lead_details", {"segment": "B2B"})
        assert lead.segment == "B2B"

    async def test_invalid_contact_details_are_reported_not_stored(self) -> None:
        lead = make_lead()
        ctx = SalesToolContext(
            db=None, tenant_id=str(lead.tenant_id), lead=lead, channel=Channel.WHATSAPP
        )
        result = await execute_tool(
            ctx, "save_lead_details", {"email": "nope", "phone": "abc"}
        )
        assert lead.email is None
        assert lead.phone_e164 is None
        assert "email" in result["rejected"]
        assert "phone" in result["rejected"]

    async def test_unknown_qualification_keys_are_dropped(self) -> None:
        """An allowlist, so a hallucinated field never lands in the CRM."""
        lead = make_lead()
        ctx = SalesToolContext(
            db=None, tenant_id=str(lead.tenant_id), lead=lead, channel=Channel.WHATSAPP
        )
        await execute_tool(
            ctx, "save_lead_details", {"favourite_colour": "blue", "need": "تدريب"}
        )
        assert "favourite_colour" not in lead.qualification
        assert lead.qualification["need"] == "تدريب"

    async def test_opt_in_is_only_ever_turned_on_and_only_explicitly(self) -> None:
        lead = make_lead()
        ctx = SalesToolContext(
            db=None, tenant_id=str(lead.tenant_id), lead=lead, channel=Channel.WHATSAPP
        )
        await execute_tool(ctx, "save_lead_details", {"marketing_opt_in": False})
        assert lead.marketing_opt_in is False
        assert lead.opt_in_at is None

        await execute_tool(ctx, "save_lead_details", {"marketing_opt_in": True})
        assert lead.marketing_opt_in is True
        assert lead.opt_in_at is not None
        assert lead.opt_in_source is not None

    async def test_unknown_tool_is_reported_not_raised(self) -> None:
        lead = make_lead()
        ctx = SalesToolContext(
            db=None, tenant_id=str(lead.tenant_id), lead=lead, channel=Channel.WHATSAPP
        )
        assert "error" in await execute_tool(ctx, "definitely_not_a_tool", {})


class TestHandoffAndDemo:
    async def test_handoff_is_recorded_on_the_context(self) -> None:
        lead = make_lead()
        ctx = SalesToolContext(
            db=None, tenant_id=str(lead.tenant_id), lead=lead, channel=Channel.WHATSAPP
        )
        await execute_tool(
            ctx,
            "request_human_handoff",
            {"reason": "DISCOUNT_REQUEST", "urgency": "HIGH", "summary": "wants 30% off"},
        )
        assert ctx.handoff is not None
        assert ctx.handoff["reason"] == "DISCOUNT_REQUEST"
        assert ctx.handoff["urgency"] == "HIGH"

    async def test_demo_defaults_the_contact_method_to_the_channel(self) -> None:
        lead = make_lead()
        ctx = SalesToolContext(
            db=None, tenant_id=str(lead.tenant_id), lead=lead, channel=Channel.WHATSAPP
        )
        await execute_tool(
            ctx, "book_demo", {"preferred_time": "بكرة الصبح", "contact_method": "junk"}
        )
        assert ctx.demo is not None
        assert ctx.demo["contact_method"] == "WHATSAPP"

    async def test_demo_warns_when_we_have_no_way_to_reach_them(self) -> None:
        lead = make_lead()
        ctx = SalesToolContext(
            db=None, tenant_id=str(lead.tenant_id), lead=lead, channel=Channel.WHATSAPP
        )
        result = await execute_tool(
            ctx, "book_demo", {"preferred_time": "الخميس", "contact_method": "PHONE"}
        )
        assert result["note"] is not None


# ----------------------------------------------------------------------
# The loop
# ----------------------------------------------------------------------
class TestGenerateReply:
    async def test_plain_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        completions = FakeCompletions([FakeMessage(content="اهلا بيك! تحت أمرك.")])
        install(monkeypatch, completions)

        reply = await agent_module.generate_reply(
            db=None,
            lead=make_lead(),
            conversation=None,
            channel=Channel.WHATSAPP,
            message="السلام عليكم",
            history=[],
        )
        assert reply.ok is True
        assert reply.text == "اهلا بيك! تحت أمرك."
        assert reply.handoff is None

    async def test_system_prompt_and_history_are_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        completions = FakeCompletions([FakeMessage(content="ok")])
        install(monkeypatch, completions)

        await agent_module.generate_reply(
            db=None,
            lead=make_lead(full_name="Sara"),
            conversation=None,
            channel=Channel.WHATSAPP,
            message="tell me more",
            history=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}],
        )
        messages = completions.calls[0]["messages"]
        assert messages[0]["role"] == "system"
        assert "Sara" in messages[0]["content"]
        assert messages[-1] == {"role": "user", "content": "tell me more"}
        assert len(messages) == 4

    async def test_tool_call_then_answer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tool_call = FakeToolCall(
            id="call_1",
            function=FakeFunction(
                name="save_lead_details",
                arguments=json.dumps({"need": "تدريب فريق المبيعات"}),
            ),
        )
        completions = FakeCompletions(
            [
                FakeMessage(content=None, tool_calls=[tool_call]),
                FakeMessage(content="تمام، سجلت ده."),
            ]
        )
        install(monkeypatch, completions)

        lead = make_lead()
        reply = await agent_module.generate_reply(
            db=None,
            lead=lead,
            conversation=None,
            channel=Channel.WHATSAPP,
            message="عايز أدرب فريق المبيعات",
            history=[],
        )
        assert reply.text == "تمام، سجلت ده."
        assert lead.qualification["need"] == "تدريب فريق المبيعات"
        assert reply.tool_calls[0]["tool"] == "save_lead_details"
        assert reply.captured["need"] == "تدريب فريق المبيعات"

        # The tool result must be fed back as a tool-role message.
        second_call_messages = completions.calls[1]["messages"]
        assert any(m.get("role") == "tool" for m in second_call_messages)

    async def test_malformed_tool_arguments_do_not_break_the_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool_call = FakeToolCall(
            id="call_bad",
            function=FakeFunction(name="save_lead_details", arguments="{not json"),
        )
        completions = FakeCompletions(
            [
                FakeMessage(content=None, tool_calls=[tool_call]),
                FakeMessage(content="تمام."),
            ]
        )
        install(monkeypatch, completions)

        reply = await agent_module.generate_reply(
            db=None,
            lead=make_lead(),
            conversation=None,
            channel=Channel.WHATSAPP,
            message="hi",
            history=[],
        )
        assert reply.text == "تمام."
        assert reply.tool_calls[0]["ok"] is False

    async def test_tool_budget_is_bounded_and_still_answers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A model that keeps searching must not turn one message into a bill."""
        monkeypatch.setattr(
            agent_module.settings, "sales_max_tool_rounds", 3, raising=False
        )
        looping_call = FakeToolCall(
            id="call_loop",
            function=FakeFunction(name="save_lead_details", arguments="{}"),
        )
        completions = FakeCompletions(
            [FakeMessage(content=None, tool_calls=[looping_call])] * 10
            + [FakeMessage(content="Answering with what I have.")]
        )
        install(monkeypatch, completions)

        reply = await agent_module.generate_reply(
            db=None,
            lead=make_lead(),
            conversation=None,
            channel=Channel.WHATSAPP,
            message="hi",
            history=[],
        )
        assert reply.text
        assert len(completions.calls) <= 3
        # The last request must withhold the tools, or the loop could not end.
        assert not completions.calls[-1].get("tools")

    async def test_model_failure_becomes_a_handoff_not_silence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeCompletions([], raises=RuntimeError("upstream 500")))

        reply = await agent_module.generate_reply(
            db=None,
            lead=make_lead(locale="ar"),
            conversation=None,
            channel=Channel.WHATSAPP,
            message="hi",
            history=[],
        )
        assert reply.ok is False
        assert reply.text
        assert reply.handoff is not None
        assert reply.handoff["urgency"] == "HIGH"

    async def test_empty_model_output_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeCompletions([FakeMessage(content="   ")]))

        reply = await agent_module.generate_reply(
            db=None,
            lead=make_lead(locale="en"),
            conversation=None,
            channel=Channel.WHATSAPP,
            message="hi",
            history=[],
        )
        assert reply.ok is False
        assert "technical" in reply.text.lower()
        assert reply.handoff is not None

    async def test_fallback_language_follows_the_lead(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install(monkeypatch, FakeCompletions([], raises=RuntimeError("boom")))
        arabic = await agent_module.generate_reply(
            db=None,
            lead=make_lead(locale="ar"),
            conversation=None,
            channel=Channel.WHATSAPP,
            message="hi",
            history=[],
        )
        assert any("؀" <= ch <= "ۿ" for ch in arabic.text)


class TestLeadSummary:
    def test_flags_an_opted_out_lead_to_the_model(self) -> None:
        from datetime import datetime, timezone

        from app.sales.qualification import score_lead, signals_from_lead

        lead = make_lead(opt_out_at=datetime.now(timezone.utc))
        summary = agent_module.summarize_lead(
            lead, score_lead(signals_from_lead(lead))
        )
        assert "OPTED OUT" in summary

    def test_lists_what_we_already_know(self) -> None:
        from app.sales.qualification import score_lead, signals_from_lead

        lead = make_lead(qualification={"need": "leadership training"})
        summary = agent_module.summarize_lead(
            lead, score_lead(signals_from_lead(lead))
        )
        assert "leadership training" in summary
