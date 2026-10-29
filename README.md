# Codex Server Tasks

English | [Chinese](README.zh-CN.md)

A skill-based Codex plugin that runs independent tasks on your own Linux server. Submit a task in Codex, disconnect your desktop, and return to inspect results or continue the same server-side Codex session.

Use `$server-tasks` in your existing Codex App. A systemd user service manages each task on the server, so disconnecting, quitting the client, or shutting down your desktop does not stop it. No second desktop application or new public port is required.

**This plugin manages independent server tasks; it does not migrate or take over the current desktop chat.** You must submit tasks explicitly. The full context of your current chat is not sent automatically, and results are not pushed back automatically.

## 1. Clone and install in one step

First, prepare Git, Python 3.10+, and a Codex CLI version that supports `codex plugin` commands on **the host where Codex runs shell commands**. If the CLI is missing and Node.js/npm is already installed, run `npm install -g @openai/codex`. If the Codex App's shell tools already run on your remote server, install the plugin there. If the tools run on your local computer, install the plugin locally and submit tasks over SSH.

### Linux / macOS / Bash

Copy the entire block into a terminal to clone this repository and install the plugin:

```bash
(
  set -eu
  command -v git >/dev/null || { echo 'Install Git first'; exit 1; }
  command -v codex >/dev/null || { echo 'Install Codex CLI first'; exit 1; }
  command -v python3 >/dev/null || { echo 'Install Python 3.10+ first'; exit 1; }
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10+ is required")'
  codex plugin --help >/dev/null
  server_tasks_repo_url='https://github.com/FitHubWHY/chatgpt-server-tasks.git'
  git clone -- "$server_tasks_repo_url" codex-server-tasks
  cd codex-server-tasks
  codex plugin marketplace add "$PWD"
  codex plugin add codex-server-tasks@codex-server-tasks
  echo 'Installed. Open a new chat in Codex App and use $server-tasks.'
)
```

Source repository: [FitHubWHY/chatgpt-server-tasks](https://github.com/FitHubWHY/chatgpt-server-tasks). Run this from a location where you can create a `codex-server-tasks` directory. If that directory already exists, `git clone` stops without overwriting it.

### Windows / PowerShell

Install Git, Python 3.10+, and Codex CLI locally first. SSH mode also requires an OpenSSH client. Copy the entire block:

```powershell
& {
    $ErrorActionPreference = 'Stop'
    Get-Command git, codex, python | Out-Null
    python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required' }
    codex plugin --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Update Codex CLI to a version that supports plugin commands' }
    $serverTasksRepoUrl = 'https://github.com/FitHubWHY/chatgpt-server-tasks.git'
    git clone -- $serverTasksRepoUrl codex-server-tasks
    if ($LASTEXITCODE -ne 0) { throw 'Clone failed. Check the URL, network, and existing directory' }
    $serverTasksDirectory = (Resolve-Path .\codex-server-tasks).Path
    codex plugin marketplace add $serverTasksDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Failed to register the plugin marketplace' }
    codex plugin add codex-server-tasks@codex-server-tasks
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install the plugin' }
    Write-Host 'Installed. Open a new chat in Codex App and use $server-tasks.'
}
```

### Install from a downloaded directory

If you used GitHub's Download ZIP option, extract the archive and enter the directory containing `plugin.json`, then run:

```bash
codex plugin marketplace add "$PWD"
codex plugin add codex-server-tasks@codex-server-tasks
```

Keep the hidden `.agents` and `.codex-plugin` directories. Open a new Codex chat after installation. If the skill does not appear, reopen Codex App. The plugin needs no MCP server or additional Python packages.

The blocks above **clone and install the plugin**. Complete server authentication, linger, and SSH setup once before first use, as described below.

## 2. Prepare your Linux server

Run the following commands on **the Linux server that will stay on**, using the same account you will use for SSH. If the server is already configured, check the requirements and skip completed steps.

```bash
python3 --version
codex --version
codex login status
```

If Codex CLI is not installed on the server, run this in an environment with Node.js/npm:

```bash
npm install -g @openai/codex
```

If you are not signed in, start device authentication and follow the URL and code shown in the terminal:

```bash
codex login --device-auth
codex login status
```

Then check the systemd user manager and linger:

```bash
systemctl --user show-environment >/dev/null
loginctl show-user "$(id -u)" --property=Linger --value
```

The second command must print `yes`. If it prints `no`, have someone with administrator privileges run this on the server:

```bash
sudo loginctl enable-linger "$(id -un)"
```

Do not run Codex or plugin tasks with sudo. Enable linger for the ordinary user who will run tasks. If `systemctl --user` cannot connect, configure that user's systemd user manager first. The plugin's current task management method requires systemd and cannot run directly on a server or container without it.

## 3. Configure SSH and check readiness

### Codex tools already run on the server

No additional SSH connection is needed. Run this from the plugin source directory:

```bash
python3 skills/server-tasks/scripts/client.py --local-server doctor
```

### Codex tools run on your local computer

Configure your server in your local `~/.ssh/config` file (`.ssh/config` under your user directory on Windows), for example:

```sshconfig
Host my-server
    HostName YOUR_SERVER_IP
    User YOUR_SERVER_USER
    IdentityFile ~/.ssh/id_ed25519
```

Replace the server address and username, and use an existing working key. Connect manually once to verify the server's host key, then confirm that future connections can run without interaction:

```bash
ssh my-server
```

Run `exit` on the server to return to your local shell, then run these commands from the plugin source directory:

```bash
ssh -T -o BatchMode=yes my-server 'python3 --version'
python3 skills/server-tasks/scripts/client.py --host my-server doctor
```

In Windows PowerShell, replace `python3` with `python` in local commands if that is your Python command. Remote commands should still use `python3` as appropriate for the server. Load passphrase-protected keys into your SSH agent beforehand; the plugin does not open a passphrase prompt.

When `doctor` returns `"ready": true`, the runtime requirements pass. Once you have also checked server authentication as described above, you can submit tasks. `doctor` does not call a model or check whether login credentials are valid. If the remote non-interactive PATH does not include Codex, run `command -v codex` on the server to find its absolute path, then specify it:

```bash
python3 skills/server-tasks/scripts/client.py --host my-server \
  --codex-bin /path/to/codex doctor
```

Tell the current chat your host alias and any custom Codex path so later operations use the same configuration. SSH mode automatically caches the runner on the server. You do not need another desktop app or a manual clone of this plugin on the server.

## 4. Use the plugin in Codex

Open a new chat and check the execution mode you intend to use:

```text
$server-tasks Check whether SSH host my-server is ready to run background tasks.
```

If the tools already run on the target server, use:

```text
$server-tasks Check whether the current server is ready to run background tasks.
```

After the check passes, submit a specific task. The working directory must already exist on the server:

```text
$server-tasks On SSH host my-server, run this in the background in /path/to/your/repository: read the project structure and summarize the entry points without modifying files. Return the job ID.
```

Once you receive a `job_id`, you can disconnect, quit the client, or shut down your desktop while the server continues. Reopen Codex and query or continue tasks on the same server:

```text
$server-tasks Show the progress and results of recent tasks on SSH host my-server.
$server-tasks Show the result of task <job_id> on SSH host my-server.
$server-tasks Continue task <job_id> on SSH host my-server: analyze the test entry points in more detail.
$server-tasks Stop task <job_id> on SSH host my-server.
```

If your tools run directly on the server, replace "SSH host my-server" with "the current server". Always use the same host and execution user for a task. If you set a custom `--state-dir`, use the same state directory as well.

Query a running task first. Use "continue" only after the original task has finished or you have explicitly stopped it. Task context comes from the submitted prompt: state the goal, permitted scope of changes, project constraints, and completion criteria clearly.

## Requirements

- Execution server: Linux, Python 3.10+, and an installed, authenticated Codex CLI.
- Working `systemd-run --user` and user linger, so the user manager remains active after SSH logout.
- Client: Python 3.10+; SSH mode also requires OpenSSH. Windows clients use SSH mode, and the execution server must still run Linux.
- `doctor` checks requirements; it does not enable linger, install dependencies, or change system login configuration.
- The server must remain powered on and connected to the network. A server restart interrupts tasks. The plugin does not automatically replay writes whose outcome is unknown; inspect the state and continue explicitly if appropriate.

If linger is not configured, ask your server administrator to enable it using their normal administration process. The plugin does not invoke sudo or modify existing services.

## Command line

Run these commands from the repository root. `--cwd` must be an existing absolute directory on the server.

```bash
python3 skills/server-tasks/scripts/client.py --local-server doctor
python3 skills/server-tasks/scripts/client.py --local-server submit \
  --cwd /path/to/your/repository --sandbox read-only \
  --request-id example-analysis-001 --prompt-file - <<'TASK'
Read the project structure and summarize the code entry points without modifying files.
TASK
python3 skills/server-tasks/scripts/client.py --local-server list
python3 skills/server-tasks/scripts/client.py --local-server status <job_id>
python3 skills/server-tasks/scripts/client.py --local-server events <job_id> --after-seq 0
python3 skills/server-tasks/scripts/client.py --local-server resume <job_id> \
  --request-id example-analysis-002 --prompt 'Continue analyzing the test entry points.'
python3 skills/server-tasks/scripts/client.py --local-server cancel <job_id>
```

Remote examples:

```bash
python3 skills/server-tasks/scripts/client.py --host my-server doctor
python3 skills/server-tasks/scripts/client.py --host my-server \
  --codex-bin /path/to/codex submit --cwd /path/to/your/repository \
  --request-id example-remote-001 --prompt 'Summarize the project structure without modifying files.' --sandbox read-only
```

Use `--codex-bin` to specify the absolute Codex path when it is missing from the remote non-interactive shell's PATH.

## Execution and recovery

1. The client sends a request to the server and receives a `job_id`. An independent systemd user service runs each task.
2. SQLite stores task state and events with sequence cursors. Raw output and final results remain on the server.
3. The service continues after the desktop client exits. Use `status` to inspect results and `events` with `next_seq` to read incremental updates.
4. `resume` creates a new job and continues the server session using the saved native Codex `thread_id`.

`request-id` deduplicates submissions. If a network failure leaves the result uncertain, reuse the same ID and content instead of submitting again with a new ID. `queued` only means the request was accepted; only `completed` means that turn finished normally. Failed execution, missing processes, and cancellation are not reported as success. You cannot resume the same Codex session concurrently.

The default sandbox is `workspace-write`; use `read-only` for analysis. Background tasks cannot open desktop approval dialogs. Operations outside the sandbox fail rather than bypass permissions automatically. Stopping a task stops only its plugin service and preserves the code directory and other sessions.

## Data location

Server state defaults to `$XDG_STATE_HOME/codex-server-tasks`, or `~/.local/state/codex-server-tasks` when `XDG_STATE_HOME` is unset. Use `--state-dir` to select another private absolute directory.

State includes prompts, events, and output. Startup briefly stores an environment snapshot with mode `0600`; the worker deletes it after reading. Task directories and the database are restricted to the current user. Keep runtime state directories, environment files, Codex login directories, and SSH keys out of public repositories. This repository contains only plugin source, documentation, a license, and isolated tests.

## Tests

No dependency download or real model call is required:

```bash
python3 tests/test_server_tasks.py -v
```

Nine isolated tests cover task execution continuing after the submission process exits, request deduplication, failure detection, session recovery, event pagination across tasks, targeted cancellation, and SSH argument isolation. They use temporary fake Codex and systemd executables and clean up afterward.

Previous development checks also exercised real systemd and Codex CLI with read-only tasks: a submitted job returned the expected marker after the submit command exited, and resuming the same `thread_id` returned a continuation marker. No end-to-end test has been performed with a real SSH disconnection or a closed desktop app.

## License

[MIT](LICENSE). This is an independent community plugin with no affiliation with OpenAI.
