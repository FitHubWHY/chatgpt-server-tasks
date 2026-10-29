#!/usr/bin/env python3
"""Codex skill transport: local server or OpenSSH, without a separate app."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import shlex
import subprocess
import sys


# Source is cached under its digest so a client update cannot replace a running
# worker's executable. Only the JSON request is passed across stdin.
BOOTSTRAP = r'''
import base64, hashlib, json, os, pathlib, subprocess, sys, tempfile
packet = json.load(sys.stdin)
source = base64.b64decode(packet["source"], validate=True)
digest = hashlib.sha256(source).hexdigest()
os.umask(0o077)
root = pathlib.Path.home() / ".cache" / "codex-server-tasks" / "runners"
root.mkdir(parents=True, exist_ok=True, mode=0o700)
target = root / (digest + ".py")
if not target.exists() or target.read_bytes() != source:
    fd, name = tempfile.mkstemp(prefix=".upload-", dir=root)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(source)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
result = subprocess.run([sys.executable, str(target), "rpc"],
    input=json.dumps(packet["request"]), text=True)
sys.exit(result.returncode)
'''


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--host", help="Existing SSH host alias or user@host")
    target.add_argument("--local-server", action="store_true",
                        help="Tools already run on the intended always-on Linux server")
    p.add_argument("--state-dir", help="Private absolute state directory on the execution server")
    p.add_argument("--codex-bin", help="Codex executable path on the execution server")
    p.add_argument("--timeout", type=int, default=45, help="Request timeout; does not limit job runtime")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor")
    listing = sub.add_parser("list")
    listing.add_argument("--limit", type=int, default=20)
    for action in ("status", "events", "cancel"):
        command = sub.add_parser(action)
        command.add_argument("job_id")
        if action == "events":
            command.add_argument("--after-seq", type=int, default=0)
            command.add_argument("--limit", type=int, default=100)
    for action in ("submit", "resume"):
        command = sub.add_parser(action)
        if action == "resume":
            command.add_argument("job_id")
        else:
            command.add_argument("--cwd", required=True)
            command.add_argument("--sandbox", choices=("read-only", "workspace-write"),
                                 default="workspace-write")
            command.add_argument("--model")
        command.add_argument("--request-id", required=True,
                             help="Keep the same ID when retrying an uncertain submission")
        prompt = command.add_mutually_exclusive_group(required=True)
        prompt.add_argument("--prompt-file", help="UTF-8 prompt file, or - for stdin")
        prompt.add_argument("--prompt")
    return p


def make_request(args: argparse.Namespace) -> dict:
    request = {"action": args.action}
    for field in ("state_dir", "codex_bin", "job_id", "cwd", "sandbox", "model",
                  "request_id", "limit", "after_seq"):
        value = getattr(args, field, None)
        if value is not None:
            request[field] = value
    if args.action in {"submit", "resume"}:
        if args.prompt_file:
            request["prompt"] = (sys.stdin.read() if args.prompt_file == "-"
                                 else Path(args.prompt_file).read_text(encoding="utf-8"))
        else:
            request["prompt"] = args.prompt
    return request


def invoke(request: dict, *, host: str | None, timeout: int) -> subprocess.CompletedProcess:
    server = Path(__file__).with_name("server_tasks.py")
    if host is None:
        command = [sys.executable, str(server), "rpc"]
        payload = request
    else:
        if not host or host.startswith("-") or any(c.isspace() for c in host):
            raise ValueError("SSH host must be a configured alias or user@host, without options")
        # SSH joins command arguments into shell text. Quote the *whole* Python
        # program, and keep request/prompt content in stdin, never in that text.
        remote = "python3 -c " + shlex.quote(BOOTSTRAP)
        command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", host, remote]
        source = server.read_bytes()
        payload = {"source": base64.b64encode(source).decode("ascii"), "request": request}
    return subprocess.run(command, input=json.dumps(payload, ensure_ascii=False),
                          text=True, capture_output=True, timeout=timeout)


def main() -> int:
    args = parser().parse_args()
    request = None
    try:
        if not 1 <= args.timeout <= 300:
            raise ValueError("timeout must be between 1 and 300 seconds")
        request = make_request(args)
        result = invoke(request, host=args.host, timeout=args.timeout)
        # SSH diagnostics are only shown on failure. The backend's JSON carries
        # application errors and is kept intact for Codex to interpret.
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.returncode and result.stderr:
            sys.stderr.write(result.stderr[-4000:])
        if result.returncode and not result.stdout:
            print(json.dumps({"error": "Server request failed; execution outcome may be unknown",
                              "request_id": request.get("request_id"),
                              "retry": "Reuse the same request_id; do not submit a duplicate task"},
                             ensure_ascii=False))
        return result.returncode if 0 <= result.returncode <= 125 else 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc),
                          "request_id": (request or {}).get("request_id"),
                          "retry": "For submit/resume, reuse the same request_id after checking status"},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
