"""Run the golden set against a live agent and report what failed.

Drives `/v1/sales/preview/chat`, which runs the real inbound path — the same
prompt, tools, policy and model a prospect on WhatsApp gets. Nothing here
reimplements the agent, so a passing eval is evidence about the deployed
service rather than about a test harness that resembles it.

    docker compose up -d
    uv run python -m evals.runner --base-url http://localhost:8000

Needs real Azure credentials, so it is NOT part of the CI gate: the workflow
runs on fake keys and cannot call a model. `tests/test_evals.py` keeps the
scorers themselves honest offline. Run this before a prompt change ships, and
on a schedule if the model version can move underneath you.

Exit code is 1 if any case fails, so it can gate a deploy script.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from evals.cases import CASES, UNIVERSAL, Case
from evals.scorers import Score, TurnResult


class Transport(Protocol):
    """How a message reaches the agent. Swapped for a stub in the tests."""

    async def send(self, session_id: str, message: str, channel: str) -> TurnResult: ...


class HttpTransport:
    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    async def send(self, session_id: str, message: str, channel: str) -> TurnResult:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/v1/sales/preview/chat",
                headers={"X-Internal-Api-Key": self._api_key},
                json={
                    "session_id": session_id,
                    "message": message,
                    "channel": channel,
                },
            )
            response.raise_for_status()
            body: dict[str, Any] = response.json()

        return TurnResult(
            reply=body.get("reply") or "",
            tool_calls=list(body.get("tool_calls") or []),
            citations=list(body.get("citations") or []),
            handoff=body.get("handoff"),
            stage=body.get("stage") or "",
            score=int(body.get("score") or 0),
            agent_ok=bool(body.get("agent_ok", True)),
        )


@dataclass
class CaseReport:
    case: Case
    scores: list[tuple[str, Score]]
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(s.passed for _, s in self.scores)

    @property
    def failures(self) -> list[tuple[str, Score]]:
        return [(name, s) for name, s in self.scores if not s.passed]


def _name(scorer: Any) -> str:
    return getattr(scorer, "__name__", repr(scorer))


async def run_case(
    case: Case, transport: Transport, channel: str = "WHATSAPP"
) -> CaseReport:
    # A fresh session per case: one conversation's history must never change
    # the grade of another, and a rerun must not inherit the last run's state.
    session_id = f"eval-{case.id}-{uuid.uuid4().hex[:8]}"
    scores: list[tuple[str, Score]] = []

    try:
        result: Optional[TurnResult] = None
        for index, message in enumerate(case.turns):
            result = await transport.send(session_id, message, channel)
            # `always` scorers apply to every turn; the rest grade the last one.
            for scorer in case.always:
                score = scorer(result)
                scores.append((f"{_name(scorer)}@turn{index + 1}", score))
        if result is None:
            return CaseReport(case, scores, error="case has no turns")
    except Exception as exc:  # noqa: BLE001 — the report is the error channel
        return CaseReport(case, scores, error=f"{type(exc).__name__}: {exc}")

    for scorer in [*UNIVERSAL, *case.checks]:
        scores.append((_name(scorer), scorer(result)))
    return CaseReport(case, scores)


async def run_all(
    transport: Transport, channel: str = "WHATSAPP", concurrency: int = 4
) -> list[CaseReport]:
    """Cases are independent, so run a few at once — but not all of them.

    Unbounded concurrency here just trips the model provider's rate limit and
    turns a real failure into a retry storm.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(case: Case) -> CaseReport:
        async with semaphore:
            return await run_case(case, transport, channel)

    return await asyncio.gather(*(guarded(case) for case in CASES))


def report(reports: list[CaseReport]) -> int:
    failed = [r for r in reports if not r.passed]

    for r in sorted(reports, key=lambda r: r.case.id):
        mark = "PASS" if r.passed else "FAIL"
        print(f"[{mark}] {r.case.id}")
        if r.passed:
            continue
        print(f"       rule: {r.case.rule}")
        if r.error:
            print(f"       error: {r.error}")
        for name, score in r.failures:
            print(f"       {name}: {score.detail}")

    total = len(reports)
    print(f"\n{total - len(failed)}/{total} passed")
    if failed:
        print("failed: " + ", ".join(sorted(r.case.id for r in failed)))

    # Distinguish "the agent is down" from "the agent broke the rules". They
    # look identical in a list of red lines, and the fix for each is nothing
    # like the fix for the other.
    dead = [r for r in reports if any(n == "agent_actually_ran" for n, _ in r.failures)]
    if dead:
        print(
            f"\n!! {len(dead)}/{total} cases never reached the model — generation "
            "failed and the service returned its fallback reply.\n"
            "   This is an OUTAGE, not a set of rule violations. Check the model "
            "credentials and endpoint,\n"
            "   then re-run. Nothing about the prompt has been measured."
        )
    return 1 if failed else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("INTERNAL_API_KEY", ""),
        help="defaults to $INTERNAL_API_KEY",
    )
    parser.add_argument(
        "--channel",
        default="WHATSAPP",
        help="channel to imitate; WHATSAPP is what most prospects use",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--case", action="append", help="run only these case ids")
    args = parser.parse_args(argv)

    if not args.api_key:
        print(
            "No API key. Pass --api-key or set INTERNAL_API_KEY.",
            file=sys.stderr,
        )
        return 2

    transport = HttpTransport(args.base_url, args.api_key)

    if args.case:
        from evals.cases import by_id

        selected = [by_id(c) for c in args.case]
        reports = asyncio.run(
            asyncio.gather(*(run_case(c, transport, args.channel) for c in selected))
        )
        return report(list(reports))

    return report(asyncio.run(run_all(transport, args.channel, args.concurrency)))


if __name__ == "__main__":
    raise SystemExit(main())
