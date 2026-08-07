"""FastAPI application.

Two surfaces with very different threat models, so they are separate routers:

* ``/v1/sales/webhooks/*`` — public, authenticated by Meta's HMAC signature.
* ``/v1/sales/*`` — internal, authenticated by a shared secret, called only by
  the LMS backend.

No CORS middleware on purpose. Nothing here is called from a browser: the web CRM
goes through the LMS, which holds the shared secret. Adding CORS would only make
it possible to leak that boundary.
"""

import logging

from fastapi import FastAPI

from app.routers import admin, health, webhooks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(
    title="OptimaLearn Sales Agent",
    description=(
        "AI sales agent for OptimaLearn: inbound qualification on WhatsApp and "
        "Facebook Messenger, compliant outbound cadences, and in-product upsell."
    ),
    version="1.0.0",
)

app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(admin.router)
