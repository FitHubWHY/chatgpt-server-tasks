"""Core durability and transport checks with temporary fake external programs.

The supervisor fixture launches real detached Python workers, but is deliberately
not a replacement for testing an actual systemd user manager or SSH server.
No model, external network, production service, or user configuration is used.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/server-tasks/scripts"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


server = load_module("server_tasks_under_test", SCRIPTS / "server_tasks.py")
client = load_module("server_tasks_client_under_test", SCRIPTS / "client.py")


FAKE_TOOLS = r'''
import json, os, pathlib, signal, subprocess, sys, time

root = pathlib.Path(os.environ["SERVER_TASK_TEST_ROOT"])
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]

def append(name, value):
    with (root / name).open("a", encoding="utf-8") as output:
        output.write(json.dumps(value) + "\n")

if name == "loginctl":
    print("yes")
elif name == "systemd-run":
    unit = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--unit="))
    first = next(index for index, arg in enumerate(args) if not arg.startswith("--"))
    command = args[first:]
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    (root / "units" / (unit + ".json")).write_text(json.dumps({"pid": child.pid, "argv": command}))
    append("launches.jsonl", {"unit": unit, "pid": child.pid, "argv": command})
elif name == "systemctl":
    if "stop" in args:
        unit = args[-1]
        append("stops.jsonl", args)
        record = root / "units" / (unit + ".json")
        if record.exists():
            (root / "units" / (unit + ".stopped")).touch()
            try:
                os.killpg(json.loads(record.read_text())["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
    elif "show-environment" in args:
        pass
    elif "show" in args:
        unit = args[args.index("show") + 1]
        stopped = (root / "units" / (unit + ".stopped")).exists()
        print("LoadState=loaded")
        print("ActiveState=" + ("inactive" if stopped else "active"))
        print("SubState=" + ("dead" if stopped else "running"))
    else:
        sys.exit(2)
elif name == "codex":
    prompt = json.loads(sys.stdin.read())
    result = pathlib.Path(args[args.index("-o") + 1])
    resumed = "resume" in args
    thread_id = args[-2] if resumed else "thread-test-" + result.parent.name
    append("codex-calls.jsonl", {"argv": args, "prompt": prompt, "thread_id": thread_id})
    def event(value):
        print(json.dumps(value), flush=True)
    event({"type": "thread.started", "thread_id": thread_id})
    if prompt.get("gate"):
        gate = pathlib.Path(prompt["gate"])
        gate.with_suffix(".started").touch()
        deadline = time.monotonic() + 15
        while not gate.exists():
            if time.monotonic() > deadline:
                sys.exit(3)
            time.sleep(0.01)
    for number in range(prompt.get("events", 3)):
        event({"type": "item.completed", "item": {"number": number}})
    if prompt.get("mode") == "failed":
        event({"type": "turn.failed", "error": {"message": "synthetic failed turn"}})
    elif prompt.get("mode") != "incomplete":
        result.write_text("Finished on the server.\n", encoding="utf-8")
        event({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
    sys.exit(prompt.get("exit_code", 0))
else:
    sys.exit(2)
'''


class ServerTasksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-server-tasks-test-")
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.root / "units").mkdir()
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.state = self.root / "state"
        for name in ("codex", "systemd-run", "systemctl", "loginctl"):
            path = self.bin / name
            path.write_text("#!" + sys.executable + "\n" + FAKE_TOOLS, encoding="utf-8")
            path.chmod(0o700)
        self.env = mock.patch.dict(os.environ, {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "SERVER_TASK_TEST_ROOT": str(self.root),
        })
        self.env.start()

    def tearDown(self):
        # All runtime scripts, model fixtures and worker groups belong to this
        # TemporaryDirectory. Clean up even if an assertion interrupts a test.
        for record in (self.root / "units").glob("*.json"):
            try:
                os.killpg(json.loads(record.read_text())["pid"], signal.SIGKILL)
            except (ProcessLookupError, FileNotFoundError):
                pass
        self.env.stop()
        self.temporary.cleanup()

    def request(self, action, **fields):
        return {"action": action, "state_dir": str(self.state), **fields}

    def submit_request(self, request_id="request-1", **prompt):
        return self.request("submit", request_id=request_id, cwd=str(self.cwd),
                            codex_bin=str(self.bin / "codex"), prompt=json.dumps(prompt))

    def rpc(self, action, **fields):
        return server.rpc(self.request(action, **fields))

    def read_jsonl(self, name):
        path = self.root / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_until(self, predicate, message, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail(message)

    def stored_job(self, job_id):
        store = server.Store(str(self.state))
        try:
            return dict(store.get(job_id))
        finally:
            store.close()

    def wait_terminal(self, job_id):
        return self.wait_until(
            lambda: (row if (row := self.stored_job(job_id))["status"] in server.TERMINAL else None),
            "worker did not record a terminal state",
        )

    def test_worker_completes_after_submitter_process_exits(self):
        gate = self.root / "release"
        request = self.submit_request(gate=str(gate))
        submitter = subprocess.Popen([sys.executable, str(SCRIPTS / "server_tasks.py"), "rpc"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        output, errors = submitter.communicate(json.dumps(request), timeout=10)
        self.assertEqual(submitter.returncode, 0, errors)
        job_id = json.loads(output)["job"]["job_id"]
        self.wait_until(lambda: gate.with_suffix(".started").exists(), "detached worker did not start")
        self.assertIsNotNone(submitter.poll())
        self.assertEqual(self.stored_job(job_id)["status"], "running")
        gate.touch()
        self.assertEqual(self.wait_terminal(job_id)["status"], "completed")
        self.assertEqual(self.rpc("status", job_id=job_id)["result"], "Finished on the server.\n")

    def test_request_id_retries_do_not_repeat_and_changed_request_is_rejected(self):
        request = self.submit_request()
        first = server.rpc(request)
        second = server.rpc(request)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["job"]["job_id"], second["job"]["job_id"])
        with self.assertRaisesRegex(ValueError, "different request"):
            server.rpc({**request, "prompt": json.dumps({"changed": True})})
        self.wait_terminal(first["job"]["job_id"])
        self.assertEqual(len(self.read_jsonl("launches.jsonl")), 1)
        self.assertEqual(len(self.read_jsonl("codex-calls.jsonl")), 1)

    def test_turn_failed_is_failure_even_with_zero_exit_code(self):
        job = server.rpc(self.submit_request(mode="failed"))["job"]
        finished = self.wait_terminal(job["job_id"])
        self.assertEqual(finished["exit_code"], 0)
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error"], "synthetic failed turn")

    def test_exit_zero_without_completed_turn_is_failure(self):
        job = server.rpc(self.submit_request(mode="incomplete"))["job"]
        finished = self.wait_terminal(job["job_id"])
        self.assertEqual(finished["exit_code"], 0)
        self.assertEqual(finished["status"], "failed")
        self.assertIn("without a turn.completed", finished["error"])

    def test_resume_uses_native_thread_and_creates_an_independent_job(self):
        original = server.rpc(self.submit_request())["job"]
        parent = self.wait_terminal(original["job_id"])
        resumed = self.rpc("resume", request_id="resume-1", job_id=parent["job_id"],
                           prompt=json.dumps({"events": 1}))["job"]
        child = self.wait_terminal(resumed["job_id"])
        self.assertNotEqual(child["job_id"], parent["job_id"])
        self.assertEqual(child["parent_job_id"], parent["job_id"])
        self.assertEqual(child["thread_id"], parent["thread_id"])
        self.assertEqual(child["status"], "completed")
        call = self.read_jsonl("codex-calls.jsonl")[-1]
        self.assertEqual(call["argv"][call["argv"].index("exec") + 1], "resume")
        self.assertEqual(call["argv"][-2], parent["thread_id"])
        self.assertNotIn("--ephemeral", call["argv"])

    def test_event_cursor_has_no_duplicates_or_missing_events(self):
        gate = self.root / "release"
        job = server.rpc(self.submit_request(events=11, gate=str(gate)))["job"]
        self.wait_until(lambda: gate.with_suffix(".started").exists(), "worker did not start")
        # Interleave another job's events so global sequence gaps are expected.
        other = server.rpc(self.submit_request("request-other", events=2))["job"]
        self.wait_terminal(other["job_id"])
        gate.touch()
        self.wait_terminal(job["job_id"])
        store = server.Store(str(self.state))
        try:
            expected = [row["seq"] for row in store.db.execute(
                "SELECT seq FROM events WHERE job_id=? ORDER BY seq", (job["job_id"],))]
        finally:
            store.close()
        cursor = 0
        actual = []
        while True:
            page = self.rpc("events", job_id=job["job_id"], after_seq=cursor, limit=3)
            sequences = [event["seq"] for event in page["events"]]
            self.assertTrue(all(seq > cursor for seq in sequences))
            actual.extend(sequences)
            cursor = page["next_seq"]
            if not page["has_more"]:
                break
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), len(set(actual)))
        empty = self.rpc("events", job_id=job["job_id"], after_seq=cursor, limit=3)
        self.assertEqual(empty, {"events": [], "next_seq": cursor, "has_more": False})

    def test_cancel_stops_only_the_requested_unique_unit(self):
        first_gate, second_gate = self.root / "first", self.root / "second"
        first = server.rpc(self.submit_request("first", gate=str(first_gate)))["job"]
        second = server.rpc(self.submit_request("second", gate=str(second_gate)))["job"]
        self.wait_until(lambda: first_gate.with_suffix(".started").exists(), "first worker did not start")
        self.wait_until(lambda: second_gate.with_suffix(".started").exists(), "second worker did not start")
        cancelled = self.rpc("cancel", job_id=first["job_id"])["job"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.read_jsonl("stops.jsonl"), [
            ["--user", "stop", "codex-server-task-" + first["job_id"] + ".service"]])
        self.assertEqual(self.stored_job(second["job_id"])["status"], "running")
        second_gate.touch()
        self.assertEqual(self.wait_terminal(second["job_id"])["status"], "completed")

    def test_cancel_rejects_a_tampered_unit_name(self):
        gate = self.root / "release"
        job = server.rpc(self.submit_request(gate=str(gate)))["job"]
        self.wait_until(lambda: gate.with_suffix(".started").exists(), "worker did not start")
        store = server.Store(str(self.state))
        try:
            store.db.execute("UPDATE jobs SET unit_name=? WHERE job_id=?", ("unrelated.service", job["job_id"]))
        finally:
            store.close()
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.rpc("cancel", job_id=job["job_id"])
        self.assertEqual(self.read_jsonl("stops.jsonl"), [])
        gate.touch()
        self.assertEqual(self.wait_terminal(job["job_id"])["status"], "completed")

    def test_ssh_request_content_never_enters_the_remote_shell_command(self):
        marker = self.root / "injected"
        hostile = "$(touch " + str(marker) + "); `echo unsafe`\n'\""
        request = {"action": "submit", "prompt": hostile, "cwd": hostile, "request_id": hostile}
        completed = subprocess.CompletedProcess([], 0, stdout="{}\n", stderr="")
        with mock.patch.object(client.subprocess, "run", return_value=completed) as runner:
            client.invoke(request, host="configured-server", timeout=10)
        argv = runner.call_args.args[0]
        self.assertEqual(argv[:8], ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", "configured-server"])
        self.assertEqual(shlex.split(argv[-1]), ["python3", "-c", client.BOOTSTRAP])
        self.assertNotIn(hostile, argv[-1])
        self.assertEqual(json.loads(runner.call_args.kwargs["input"])["request"], request)
        self.assertFalse(runner.call_args.kwargs.get("shell", False))
        self.assertFalse(marker.exists())
        for host in ("-oProxyCommand=evil", "server -oProxyCommand=evil", "server\ncommand", ""):
            with self.subTest(host=host), mock.patch.object(client.subprocess, "run") as runner:
                with self.assertRaises(ValueError):
                    client.invoke(request, host=host, timeout=10)
                runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
