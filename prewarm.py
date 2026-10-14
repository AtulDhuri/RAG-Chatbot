#!/usr/bin/env python3
"""Pre-warm a site's index so no real visitor ever waits on the first crawl.

Because the chat widget lives on a client's live site, the *first visitor*
would otherwise be the one to trigger the initial crawl (the slow part). Run
this once when you onboard a site -- e.g. right after adding the widget script
-- so the index is built ahead of time. Every visitor after that, including the
first, gets instant answers.

Usage:
    python prewarm.py https://example.com
    python prewarm.py example.com another-site.com --base-url http://localhost:8000
    python prewarm.py example.com --force          # re-crawl even if fresh

It calls POST /api/register to kick off the crawl, then polls GET /api/status
until the domain reaches the "ready" phase (or errors / times out).
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx


def prewarm(
    base_url: str,
    domain: str,
    *,
    force: bool,
    timeout_s: float,
    poll_interval_s: float,
) -> bool:
    """Register a domain and wait until it's indexed. Returns True on success."""
    base_url = base_url.rstrip("/")
    print(f"[{domain}] registering...", flush=True)

    try:
        r = httpx.post(
            f"{base_url}/api/register",
            json={"domain": domain, "force": force},
            timeout=30.0,
        )
        r.raise_for_status()
        reg = r.json()
    except Exception as exc:
        print(f"[{domain}] register failed: {exc}", file=sys.stderr)
        return False

    if reg.get("indexed") and not force:
        # Already had content; a background refresh may still run, but the site
        # is already answerable, so we don't need to block.
        print(f"[{domain}] already indexed - ready.", flush=True)
        return True

    deadline = time.monotonic() + timeout_s
    last_done = -1
    while time.monotonic() < deadline:
        try:
            s = httpx.get(
                f"{base_url}/api/status",
                params={"domain": domain},
                timeout=30.0,
            ).json()
        except Exception as exc:
            print(f"[{domain}] status check failed, retrying: {exc}", file=sys.stderr)
            time.sleep(poll_interval_s)
            continue

        phase = s.get("phase", "")
        done = s.get("pages_done", 0)
        known = s.get("pages_known", 0)

        if done != last_done:
            if phase == "embedding":
                print(f"[{domain}] embedding ({done} pages read)...", flush=True)
            else:
                total = f"/{known}" if known else ""
                print(f"[{domain}] crawling {done}{total} pages...", flush=True)
            last_done = done

        if phase == "ready" or s.get("indexed"):
            print(f"[{domain}] ready ({s.get('chunks', 0)} chunks).", flush=True)
            return True
        if phase == "error":
            print(f"[{domain}] indexing errored.", file=sys.stderr)
            return False

        time.sleep(poll_interval_s)

    print(f"[{domain}] timed out after {timeout_s:.0f}s.", file=sys.stderr)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-warm site indexes for the RAG chatbot.")
    ap.add_argument("domains", nargs="+", help="One or more domains or URLs to index.")
    ap.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Backend base URL (default: http://localhost:8000).",
    )
    ap.add_argument(
        "--force", action="store_true", help="Force a re-crawl even if content is fresh."
    )
    ap.add_argument(
        "--timeout", type=float, default=600.0,
        help="Max seconds to wait per domain (default: 600).",
    )
    ap.add_argument(
        "--interval", type=float, default=2.0,
        help="Seconds between status polls (default: 2).",
    )
    args = ap.parse_args()

    all_ok = True
    for domain in args.domains:
        ok = prewarm(
            args.base_url,
            domain,
            force=args.force,
            timeout_s=args.timeout,
            poll_interval_s=args.interval,
        )
        all_ok = all_ok and ok

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
