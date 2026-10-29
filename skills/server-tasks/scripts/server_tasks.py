#!/usr/bin/env python3
"""Private, durable Codex CLI jobs supervised by the server's user systemd.

The ``rpc`` command accepts one JSON object on stdin and returns one JSON object.
No daemon, public listener, shell evaluation, or client-side process is required.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import uuid


BACKEND = "systemd-user"
ACTIVE = ("queued", "running", "launch_uncertain")
TERMINAL = ("completed", "failed", "cancelled", "interrupted")
MAX_REQUEST = 2 * 1024 * 1024
MAX_EVENT = 2 * 1024 * 1024
MAX_EVENT_RESPONSE = 4 * 1024 * 1024
MAX_RESULT = 256 * 1024
JOB_RE = re.compile(r"[0-9a-f]{32}\Z")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def text_field(request, name, *, required=False, maximum=1024):
    value = request.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{name} must be a nonempty string")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{name} is too long")
    return value


def limit_field(request, name, default, maximum):
    value = request.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return min(value, maximum)


def job_id_field(request):
    value = text_field(request, "job_id", required=True)
    if not JOB_RE.fullmatch(value):
        raise ValueError("invalid job_id")
    return value


def environment():
    env = dict(os.environ)
    brokered = any(env.get(name) for name in (
        "CODEX_NETWORK_PROXY_ACTIVE", "CODEX_NETWORK_PROXY_BROKERED_CREDENTIALS",
        "CODEX_NETWORK_PROXY_CREDENTIAL_BROKER_ACTIVE",
    ))
    for key in list(env):
        if key.startswith("CODEX_") and key not in {"CODEX_HOME", "CODEX_API_KEY"}:
            env.pop(key)
        elif key.startswith("BUNDLE_") or (brokered and key.lower().endswith("_proxy")):
            env.pop(key)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run_control(argv, timeout=15):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=timeout,
                          env=environment(), check=False)


def resolve_codex(requested=None):
    candidate = requested or "codex"
    found = shutil.which(candidate)
    if not found:
        raise ValueError("Codex CLI is unavailable on the server; install it and log in first")
    return str(Path(found).resolve())


def doctor(request):
    linux = sys.platform.startswith("linux")
    systemd_run = shutil.which("systemd-run") is not None
    systemctl = shutil.which("systemctl") is not None
    loginctl = shutil.which("loginctl") is not None
    user_manager = False
    linger = False
    if linux and systemctl:
        try:
            user_manager = run_control(["systemctl", "--user", "show-environment"]).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    if linux and loginctl:
        try:
            answer = run_control(["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"])
            linger = answer.returncode == 0 and answer.stdout.strip().lower() == "yes"
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        codex_path = resolve_codex(text_field(request, "codex_bin", maximum=4096))
    except ValueError:
        codex_path = None
    ready = linux and systemd_run and user_manager and linger and codex_path is not None
    return {"backend": BACKEND, "linux": linux, "systemd_run": systemd_run,
            "systemctl": systemctl, "loginctl": loginctl, "user_manager": user_manager,
            "linger": linger, "codex": codex_path is not None, "codex_bin": codex_path,
            "ready": ready}


class Store:
    def __init__(self, directory=None):
        default = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "codex-server-tasks"
        target = Path(directory).expanduser() if directory else default
        if not target.is_absolute():
            raise ValueError("state_dir must be an absolute path")
        if target.is_symlink():
            raise ValueError("state_dir must not be a symlink")
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = target.resolve()
        if self.root.stat().st_uid != os.getuid():
            raise ValueError("state_dir must belong to the current server user")
        self.root.chmod(0o700)
        (self.root / "jobs").mkdir(mode=0o700, exist_ok=True)
        db_path = self.root / "tasks.sqlite3"
        if db_path.is_symlink() or (self.root / ".lock").is_symlink():
            raise ValueError("state files must not be symlinks")
        self.db = sqlite3.connect(str(db_path), timeout=20, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        db_path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=20000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL, status TEXT NOT NULL,
                thread_id TEXT, parent_job_id TEXT, cwd TEXT NOT NULL,
                prompt TEXT NOT NULL, sandbox TEXT NOT NULL, model TEXT,
                codex_bin TEXT NOT NULL, unit_name TEXT NOT NULL,
                created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                error TEXT, exit_code INTEGER, cancel_requested INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(job_id), event TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_job_seq ON events(job_id,seq);
            CREATE INDEX IF NOT EXISTS jobs_thread ON jobs(thread_id,status);
        """)

    @contextlib.contextmanager
    def locked(self):
        with open(self.root / ".lock", "a", encoding="utf-8") as handle:
            os.chmod(handle.name, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("job not found")
        return row

    def event(self, job_id, value):
        self.db.execute("INSERT INTO events(job_id,event) VALUES (?,?)", (job_id, compact(value)))

    def finish(self, job_id, status, error=None, exit_code=None):
        with self.transaction():
            row = self.get(job_id)
            if row["status"] in TERMINAL:
                return
            self.db.execute("UPDATE jobs SET status=?,finished_at=?,error=?,exit_code=? WHERE job_id=?",
                            (status, now(), error, exit_code, job_id))
            self.event(job_id, {"type": "runner.finished", "status": status, "error": error})
        # Environment values are transport for systemd, never a job artifact.
        (self.root / "jobs" / job_id / "environment.json").unlink(missing_ok=True)

    def close(self):
        self.db.close()


def public_job(row):
    keys = ("job_id", "status", "thread_id", "parent_job_id", "cwd", "created_at",
            "started_at", "finished_at", "error", "exit_code")
    return {**{key: row[key] for key in keys}, "backend": BACKEND}


def unit_state(unit):
    try:
        result = run_control(["systemctl", "--user", "show", unit, "--property=LoadState",
                              "--property=ActiveState", "--property=SubState"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if "LoadState" not in values or "ActiveState" not in values:
        return None
    return values


def reconcile(store):
    rows = store.db.execute("SELECT * FROM jobs WHERE status IN (?,?,?)", ACTIVE).fetchall()
    for row in rows:
        state = unit_state(row["unit_name"])
        if state is None:
            continue
        if state["LoadState"] == "not-found" or state["ActiveState"] in ("inactive", "failed"):
            status = "cancelled" if row["cancel_requested"] else "interrupted"
            store.finish(row["job_id"], status,
                         None if status == "cancelled" else "Supervisor unit stopped without a recorded completed turn; resume explicitly if appropriate")


def submit(store, request, resume=False):
    request_id = text_field(request, "request_id", required=True, maximum=256)
    prompt = text_field(request, "prompt", required=True, maximum=1024 * 1024)
    parent_id = job_id_field(request) if resume else None
    with store.locked():
        # The idempotency lookup precedes reconciliation and dispatch. A lost SSH
        # response can never replay a prompt, including an uncertain launch.
        canonical = {key: value for key, value in request.items() if key != "state_dir"}
        digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
        old = store.db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
        if old:
            if old["request_hash"] != digest:
                raise ValueError("request_id was already used for a different request")
            return {"job": public_job(old), "duplicate": True}
        reconcile(store)
        parent = store.get(parent_id) if resume else None
        if parent is not None:
            if parent["status"] not in TERMINAL:
                raise ValueError("the parent job is still active; wait or cancel it first")
            if not parent["thread_id"]:
                raise ValueError("the parent job has no native Codex thread_id to resume")
            conflict = store.db.execute("SELECT job_id FROM jobs WHERE thread_id=? AND status IN (?,?,?)",
                                        (parent["thread_id"], *ACTIVE)).fetchone()
            if conflict:
                raise ValueError("another job is already running or queued for this Codex thread")
        cwd_value = text_field(request, "cwd", required=not resume, maximum=4096)
        cwd = Path(cwd_value or parent["cwd"])
        if not cwd.is_absolute() or not cwd.is_dir():
            raise ValueError("cwd must be an existing absolute directory on the server")
        cwd = str(cwd.resolve())
        sandbox = request.get("sandbox", parent["sandbox"] if parent else "workspace-write")
        if sandbox not in ("read-only", "workspace-write"):
            raise ValueError("sandbox must be read-only or workspace-write")
        model = text_field(request, "model", maximum=256)
        if model is None and parent:
            model = parent["model"]
        codex_bin = text_field(request, "codex_bin", maximum=4096)
        codex_bin = resolve_codex(codex_bin or (parent["codex_bin"] if parent else None))
        report = doctor({"codex_bin": codex_bin})
        if not report["ready"]:
            missing = [key for key in ("linux", "systemd_run", "user_manager", "linger", "codex") if not report[key]]
            raise ValueError("Persistent jobs require Linux, a reachable systemd user manager, enabled user linger, and Codex CLI. Unavailable: " + ", ".join(missing) + ". Ask the server administrator to configure these; this plugin never enables linger or uses sudo.")
        job_id = uuid.uuid4().hex
        unit = "codex-server-task-" + job_id + ".service"
        folder = store.root / "jobs" / job_id
        folder.mkdir(mode=0o700)
        env = environment()
        env["PATH"] = str(Path(codex_bin).parent) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
        (folder / "environment.json").write_text(compact(env), encoding="utf-8")
        (folder / "environment.json").chmod(0o600)
        with store.transaction():
            store.db.execute("""INSERT INTO jobs(job_id,request_id,request_hash,status,thread_id,
                parent_job_id,cwd,prompt,sandbox,model,codex_bin,unit_name,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, request_id, digest, "queued", parent["thread_id"] if parent else None,
                 parent_id, cwd, prompt, sandbox, model, codex_bin, unit, now()))
            store.event(job_id, {"type": "runner.queued", "backend": BACKEND})
        command = ["systemd-run", "--user", "--collect", "--quiet", "--unit=" + unit,
                   "--property=Type=exec", "--property=KillMode=control-group", "--property=UMask=0077",
                   str(Path(sys.executable).resolve()), str(Path(__file__).resolve()),
                   "_worker", str(store.root), job_id]
        try:
            dispatched = run_control(command, timeout=25)
        except subprocess.TimeoutExpired:
            with store.transaction():
                store.db.execute("UPDATE jobs SET status='launch_uncertain',error=? WHERE job_id=? AND status='queued'",
                                 ("Supervisor dispatch timed out; do not resubmit with a new request_id", job_id))
                store.event(job_id, {"type": "runner.launch_uncertain"})
        except OSError:
            store.finish(job_id, "failed", "Unable to invoke systemd-run on the server")
        else:
            if dispatched.returncode:
                # systemd may have accepted the job before a client-side error.
                # Preserve uncertainty instead of automatically retrying it.
                with store.transaction():
                    store.db.execute("UPDATE jobs SET status='launch_uncertain',error=? WHERE job_id=? AND status='queued'",
                                     ("systemd-run did not confirm dispatch; inspect job status before explicitly resuming", job_id))
                    store.event(job_id, {"type": "runner.launch_uncertain"})
        return {"job": public_job(store.get(job_id)), "duplicate": False}


def bounded_result(store, job_id):
    path = store.root / "jobs" / job_id / "result.md"
    try:
        with path.open("rb") as handle:
            value = handle.read(MAX_RESULT + 1)
    except FileNotFoundError:
        return "", False
    return value[:MAX_RESULT].decode("utf-8", errors="replace"), len(value) > MAX_RESULT


def rpc(request):
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    action = request.get("action")
    if action == "doctor":
        return doctor(request)
    if action not in ("submit", "resume", "list", "status", "events", "cancel"):
        raise ValueError("action must be doctor, submit, resume, list, status, events, or cancel")
    store = Store(text_field(request, "state_dir", maximum=4096))
    try:
        if action in ("submit", "resume"):
            return submit(store, request, resume=action == "resume")
        with store.locked():
            reconcile(store)
            if action == "list":
                limit = limit_field(request, "limit", 30, 200)
                rows = store.db.execute("SELECT * FROM jobs ORDER BY created_at DESC,job_id DESC LIMIT ?", (limit,))
                return {"jobs": [public_job(row) for row in rows]}
            job_id = job_id_field(request)
            row = store.get(job_id)
            if action == "events":
                after = request.get("after_seq", 0)
                if isinstance(after, bool) or not isinstance(after, int) or after < 0:
                    raise ValueError("after_seq must be a nonnegative integer")
                limit = limit_field(request, "limit", 100, 1000)
                rows = store.db.execute("SELECT seq,event FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?",
                                        (job_id, after, limit + 1))
                events = []
                response_size = 0
                has_more = False
                for item in rows:
                    size = len(item["event"].encode("utf-8"))
                    if len(events) >= limit or (events and response_size + size > MAX_EVENT_RESPONSE):
                        has_more = True
                        break
                    events.append({"seq": item["seq"], "event": json.loads(item["event"])})
                    response_size += size
                return {"events": events, "next_seq": events[-1]["seq"] if events else after,
                        "has_more": has_more}
            if action == "status":
                result, truncated = bounded_result(store, job_id)
                return {"job": public_job(row), "result": result, "result_truncated": truncated}
            if row["status"] in TERMINAL:
                return {"job": public_job(row)}
            # Only the unit derived from this private job's UUID can be stopped.
            expected = "codex-server-task-" + job_id + ".service"
            if row["unit_name"] != expected:
                raise ValueError("stored supervisor unit does not match this job")
            with store.transaction():
                store.db.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id=?", (job_id,))
                store.event(job_id, {"type": "runner.cancel_requested"})
            try:
                stopped = run_control(["systemctl", "--user", "stop", expected], timeout=40)
            except (OSError, subprocess.TimeoutExpired):
                raise ValueError("Cancellation could not be confirmed; inspect job status and retry cancel") from None
            if stopped.returncode:
                state = unit_state(expected)
                if state is None or (state.get("LoadState") != "not-found" and state.get("ActiveState") not in ("inactive", "failed")):
                    raise ValueError("Cancellation could not be confirmed; inspect job status and retry cancel")
            store.finish(job_id, "cancelled")
            return {"job": public_job(store.get(job_id))}
    finally:
        store.close()


def worker(directory, job_id):
    if not JOB_RE.fullmatch(job_id):
        raise ValueError("invalid worker job_id")
    store = Store(directory)
    process = None
    stopped = False

    def terminate(_signum, _frame):
        nonlocal stopped
        stopped = True
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        with store.transaction():
            row = store.get(job_id)
            if row["status"] not in ("queued", "launch_uncertain"):
                return 0
            if row["cancel_requested"]:
                return 0
            store.db.execute("UPDATE jobs SET status='running',started_at=?,error=NULL WHERE job_id=?", (now(), job_id))
            store.event(job_id, {"type": "runner.started", "backend": BACKEND})
        folder = store.root / "jobs" / job_id
        environment_path = folder / "environment.json"
        env = json.loads(environment_path.read_text(encoding="utf-8"))
        environment_path.unlink()
        command = [row["codex_bin"], "--no-daemon", "-a", "never", "--sandbox", row["sandbox"], "-C", row["cwd"]]
        if row["model"]:
            command += ["--model", row["model"]]
        command += ["exec"]
        if row["parent_job_id"]:
            command += ["resume"]
        command += ["--json", "-o", str(folder / "result.md")]
        if not row["parent_job_id"]:
            command += ["--color", "never"]
        if row["parent_job_id"]:
            command += [row["thread_id"]]
        command += ["-"]
        turn_completed = False
        turn_failed = False
        failure_message = None
        with (folder / "stdout.jsonl").open("ab") as output, (folder / "stderr.log").open("ab") as errors, \
                tempfile.TemporaryFile(dir=folder) as prompt_stream:
            # A file descriptor avoids stdin/stdout pipe deadlocks with long
            # prompts and keeps the prompt out of process arguments and logs.
            prompt_stream.write(row["prompt"].encode("utf-8"))
            prompt_stream.seek(0)
            process = subprocess.Popen(command, cwd=row["cwd"], env=env, stdin=prompt_stream,
                                       stdout=subprocess.PIPE, stderr=errors)
            if stopped and process.poll() is None:
                process.terminate()
            while True:
                line = process.stdout.readline(MAX_EVENT + 1)
                if not line:
                    break
                output.write(line)
                output.flush()
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("event is not an object")
                except (ValueError, UnicodeError):
                    event = {"type": "runner.output", "text": line[:65536].decode("utf-8", errors="replace"),
                             "truncated": len(line) > 65536}
                kind = event.get("type")
                with store.transaction():
                    if kind == "thread.started" and isinstance(event.get("thread_id"), str):
                        store.db.execute("UPDATE jobs SET thread_id=? WHERE job_id=?", (event["thread_id"], job_id))
                    store.event(job_id, event)
                if kind == "turn.completed":
                    turn_completed = True
                if kind == "turn.failed":
                    turn_failed = True
                    error = event.get("error")
                    if isinstance(error, dict):
                        failure_message = str(error.get("message", "Codex turn failed"))[:4000]
                    else:
                        failure_message = "Codex turn failed"
            exit_code = process.wait()
        current = store.get(job_id)
        if current["cancel_requested"]:
            store.finish(job_id, "cancelled", exit_code=exit_code)
        elif stopped:
            store.finish(job_id, "interrupted", "Worker was interrupted before completion", exit_code)
        elif turn_failed:
            store.finish(job_id, "failed", failure_message or "Codex turn failed", exit_code)
        elif exit_code == 0 and turn_completed:
            store.finish(job_id, "completed", exit_code=exit_code)
        else:
            reason = f"Codex exited with code {exit_code}" if exit_code else "Codex exited without a turn.completed event"
            store.finish(job_id, "failed", reason, exit_code)
        return 0
    except BaseException as exc:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        try:
            cancelled = bool(store.get(job_id)["cancel_requested"])
            store.finish(job_id, "cancelled" if cancelled else "failed",
                         None if cancelled else "Worker error: " + type(exc).__name__)
        finally:
            store.close()
        return 1
    finally:
        try:
            store.close()
        except sqlite3.ProgrammingError:
            pass


def main():
    os.umask(0o077)
    if len(sys.argv) == 4 and sys.argv[1] == "_worker":
        return worker(sys.argv[2], sys.argv[3])
    try:
        if sys.argv[1:] != ["rpc"]:
            raise ValueError("usage: server_tasks.py rpc (one JSON request on stdin)")
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError("request exceeds 2 MiB")
        result = rpc(json.loads(raw))
        print(compact(result))
        return 0
    except Exception as exc:
        # Never echo commands, prompt text, environment values or credentials.
        error = str(exc) if isinstance(exc, (ValueError, json.JSONDecodeError)) else "Server operation failed: " + type(exc).__name__
        print(compact({"error": error}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
