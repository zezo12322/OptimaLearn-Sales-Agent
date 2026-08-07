"""AI sales agent: inbound qualification, compliant outbound, in-product upsell.

Layout:

* ``enums`` — dependency-free vocabulary shared by everything below.
* ``policy`` — pure messaging-policy engine (consent, service windows, quiet
  hours, caps). No I/O, so it is fully unit-testable.
* ``qualification`` — deterministic lead scoring and stage transitions.
* ``kb`` — sales knowledge base: chunk, embed, retrieve.
* ``prompts`` / ``tools`` / ``agent`` — the conversational core.
* ``repository`` — lead identity resolution and conversation persistence.
* ``channels`` — WhatsApp and Messenger adapters behind one interface.
* ``outbound`` — durable send queue and cadence engine.
* ``upsell`` — in-product upgrade recommendations.
"""
