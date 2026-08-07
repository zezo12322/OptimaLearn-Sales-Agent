"""Assert the app imports and its critical routes are registered.

Run in CI *outside* pytest on purpose. ``tests/conftest.py`` fills in environment
defaults, so a green test suite does not prove the app starts on the variables a
real deployment actually sets. This does.

Run it as ``python -m scripts.check_app_routes``. Running the file directly puts
``scripts/`` on ``sys.path`` instead of the repo root, and ``app`` will not
import.

It checks named routes rather than counting them. A count is a weak proxy that
goes stale every time the surface legitimately changes — it broke when the
LMS-era routes were removed, reporting a problem that did not exist. The real
risk is a router that silently fails to register, taking a whole surface down
while the process still boots happily; naming the routes catches exactly that
and stays quiet about everything else.
"""

import sys

from app.main import app

#: Losing any of these is an outage, not a refactor.
REQUIRED: dict[str, set[str]] = {
    # The public surface. If these do not register, Meta's webhooks 404 and the
    # agent never sees a single message.
    "/v1/sales/webhooks/whatsapp": {"get", "post"},
    "/v1/sales/webhooks/messenger": {"get", "post"},
    # What the CRM and operators read and write.
    "/v1/sales/leads": {"get"},
    "/v1/sales/leads/{lead_id}": {"get", "patch"},
    "/v1/sales/leads/{lead_id}/messages": {"post"},
    "/v1/sales/stats": {"get"},
    "/v1/sales/knowledge": {"get", "post"},
    # Lets the agent be exercised without any Meta wiring.
    "/v1/sales/preview/chat": {"post"},
    # Liveness, for whatever runs the container.
    "/v1/health": {"get"},
    "/v1/ready": {"get"},
}


def main() -> int:
    paths = app.openapi()["paths"]
    problems: list[str] = []

    for path, methods in REQUIRED.items():
        if path not in paths:
            problems.append(f"missing route: {path}")
            continue
        registered = {m.lower() for m in paths[path]}
        if missing := methods - registered:
            problems.append(f"{path}: missing {sorted(missing)}")

    if problems:
        print("Route check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"\nRegistered paths ({len(paths)}):", file=sys.stderr)
        for path in sorted(paths):
            print(f"  {path}", file=sys.stderr)
        return 1

    operations = sum(len(methods) for methods in paths.values())
    print(f"OK — {len(paths)} paths, {operations} operations, all required present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
