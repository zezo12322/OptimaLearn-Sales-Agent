"""Internal API authentication.

A shared secret rather than JWTs: every caller is a trusted server — a server
action in the Optimatech site, or an operator holding the key. No end user ever
reaches these endpoints directly, so a rotatable header secret is the right
amount of machinery.

The comparison is constant-time. A naive ``!=`` on a secret leaks its prefix to
anyone willing to time the responses.
"""

import hmac

from fastapi import Header, HTTPException, status

from app.core.config import settings


async def verify_internal_key(x_internal_api_key: str = Header(...)) -> None:
    if not hmac.compare_digest(x_internal_api_key, settings.internal_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "INVALID_API_KEY",
                    "message": "Missing or invalid X-Internal-Api-Key header.",
                }
            },
        )
