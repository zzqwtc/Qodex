#!/usr/bin/env python3
"""Send a minimal OneBot message event to a local bridge for testing."""

from __future__ import annotations

import argparse
import json
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a test OneBot event.")
    parser.add_argument("--url", default="http://127.0.0.1:8787/onebot")
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--group-id", type=int)
    parser.add_argument("--text", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    event = {
        "post_type": "message",
        "message_type": "group" if args.group_id else "private",
        "user_id": args.user_id,
        "message": args.text,
        "raw_message": args.text,
    }
    if args.group_id:
        event["group_id"] = args.group_id

    data = json.dumps(event, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        args.url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        print(response.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
