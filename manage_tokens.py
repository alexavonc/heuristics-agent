#!/usr/bin/env python3
"""
Token management for the Heuristics Agent.

Usage:
  python manage_tokens.py add <label> [count]   # generate 1 (or N) tokens for a person
  python manage_tokens.py list                  # show all tokens and their status
  python manage_tokens.py reset <token>         # un-burn a token (give someone a retry)
  python manage_tokens.py delete <token>        # permanently remove a token

Examples:
  python manage_tokens.py add Alice           # 1 token for Alice
  python manage_tokens.py add Bob 3           # 3 tokens for Bob
  python manage_tokens.py list
  python manage_tokens.py reset abc123...
"""

import json
import os
import sys
import time
import uuid

TOKENS_FILE = os.path.join(os.path.dirname(__file__), "tokens.json")


def _load():
    try:
        with open(TOKENS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(tokens):
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    print(f"Saved → {TOKENS_FILE}")


def cmd_add(label: str, count: int = 1):
    tokens = _load()
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    new_tokens = []
    for _ in range(count):
        t = str(uuid.uuid4())
        tokens[t] = {"label": label, "used": False, "created": created, "used_at": None}
        new_tokens.append(t)
    _save(tokens)
    print(f"\nGenerated {count} token(s) for '{label}':")
    for t in new_tokens:
        print(f"  {t}")
    print()


def cmd_list():
    tokens = _load()
    if not tokens:
        print("No tokens. tokens.json is empty or missing — auth is disabled.")
        return
    used   = [(t, e) for t, e in tokens.items() if e.get("used")]
    unused = [(t, e) for t, e in tokens.items() if not e.get("used")]
    print(f"\n{'TOKEN':<38}  {'LABEL':<16}  {'STATUS':<8}  DETAIL")
    print("─" * 85)
    for t, e in sorted(unused, key=lambda x: x[1].get("created", "")):
        print(f"  {t}  {e.get('label',''):<16}  {'unused':<8}  created {e.get('created','?')}")
    for t, e in sorted(used, key=lambda x: x[1].get("used_at", "")):
        print(f"  {t}  {e.get('label',''):<16}  {'USED':<8}  used at {e.get('used_at','?')}")
    print(f"\n  {len(unused)} unused  |  {len(used)} used  |  {len(tokens)} total\n")


def cmd_reset(token: str):
    tokens = _load()
    if token not in tokens:
        print(f"Token not found: {token}")
        sys.exit(1)
    tokens[token]["used"]    = False
    tokens[token]["used_at"] = None
    _save(tokens)
    print(f"Reset: {token} (label: {tokens[token].get('label')})")


def cmd_delete(token: str):
    tokens = _load()
    if token not in tokens:
        print(f"Token not found: {token}")
        sys.exit(1)
    label = tokens.pop(token).get("label")
    _save(tokens)
    print(f"Deleted: {token} (label: {label})")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    if cmd == "add":
        if len(args) < 2:
            print("Usage: manage_tokens.py add <label> [count]")
            sys.exit(1)
        cmd_add(args[1], int(args[2]) if len(args) > 2 else 1)
    elif cmd == "list":
        cmd_list()
    elif cmd == "reset":
        if len(args) < 2:
            print("Usage: manage_tokens.py reset <token>")
            sys.exit(1)
        cmd_reset(args[1])
    elif cmd == "delete":
        if len(args) < 2:
            print("Usage: manage_tokens.py delete <token>")
            sys.exit(1)
        cmd_delete(args[1])
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
