"""Channel adapters.

Each adapter turns a provider's webhook into
:class:`~app.sales.channels.base.InboundEvent` objects and knows how to send on
that provider. Everything above this package — the agent, the policy engine, the
CRM — is channel-agnostic and works off the normalised event.

Nothing is imported eagerly here: ``base`` is pure and must stay importable
without settings or network configuration.
"""
