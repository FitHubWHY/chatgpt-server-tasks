# Codex Server Tasks · 服务器续跑

A skill-based Codex plugin that runs independent tasks on your own Linux server. Submit a task in Codex, disconnect your desktop, and return to inspect results or continue the same server-side Codex session.

在现有 Codex App 中通过 `$server-tasks` 提交任务。服务器的 systemd user service 托管执行，客户端断网、退出或关机不会终止服务器任务。无需安装第二个桌面应用，不对公网开放新端口。

**边界：这是独立服务器任务托管，不会迁移或接管当前桌面聊天。** 任务需要显式提交；当前聊天的完整上下文不会自动传给服务器，结果也不会自动推送回来。

## 1. 一键拉取代码并安装插件

先在**使用 Codex 的执行主机**上准备 Git、Python 3.10+ 和支持 `codex plugin` 命令的 Codex CLI。CLI 未安装时，可在已有 Node.js/npm 的环境执行 `npm install -g @openai/codex`。Codex App 的 shell 工具已在远程服务器运行时，就在那台服务器安装；工具仍在本地电脑运行时，在本地安装插件，后续通过 SSH 提交任务。

### Linux / macOS / Bash

将下面整段复制到终端，即可从本仓库拉取代码并安装插件：

```bash
(
  set -eu
  command -v git >/dev/null || { echo '请先安装 Git'; exit 1; }
  command -v codex >/dev/null || { echo '请先安装 Codex CLI'; exit 1; }
  command -v python3 >/dev/null || { echo '请先安装 Python 3.10+'; exit 1; }
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "需要 Python 3.10+")'
  codex plugin --help >/dev/null
  server_tasks_repo_url='https://github.com/FitHubWHY/chatgpt-server-tasks.git'
  git clone -- "$server_tasks_repo_url" codex-server-tasks
  cd codex-server-tasks
  codex plugin marketplace add "$PWD"
  codex plugin add codex-server-tasks@codex-server-tasks
  echo '安装完成，请在 Codex App 中新开聊天并使用 $server-tasks。'
)
```

源码仓库：[FitHubWHY/chatgpt-server-tasks](https://github.com/FitHubWHY/chatgpt-server-tasks)。运行位置需要能创建 `codex-server-tasks` 目录；同名目录已存在时，`git clone` 会停止，不覆盖已有文件。

### Windows / PowerShell

本地需先安装 Git、Python 3.10+、Codex CLI；使用 SSH 模式时还需 OpenSSH 客户端。复制以下整段即可：

```powershell
& {
    $ErrorActionPreference = 'Stop'
    Get-Command git, codex, python | Out-Null
    python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
    if ($LASTEXITCODE -ne 0) { throw '需要 Python 3.10+' }
    codex plugin --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '当前 Codex CLI 不支持插件命令，请先更新' }
    $serverTasksRepoUrl = 'https://github.com/FitHubWHY/chatgpt-server-tasks.git'
    git clone -- $serverTasksRepoUrl codex-server-tasks
    if ($LASTEXITCODE -ne 0) { throw '拉取失败，请检查地址、网络及同名目录' }
    $serverTasksDirectory = (Resolve-Path .\codex-server-tasks).Path
    codex plugin marketplace add $serverTasksDirectory
    if ($LASTEXITCODE -ne 0) { throw '注册插件源失败' }
    codex plugin add codex-server-tasks@codex-server-tasks
    if ($LASTEXITCODE -ne 0) { throw '安装插件失败' }
    Write-Host '安装完成，请在 Codex App 中新开聊天并使用 $server-tasks。'
}
```

### 已经下载目录

如果是通过 GitHub 的 Download ZIP 下载，先解压并进入包含 `plugin.json` 的目录，再执行：

```bash
codex plugin marketplace add "$PWD"
codex plugin add codex-server-tasks@codex-server-tasks
```

请保留 `.agents` 和 `.codex-plugin` 隐藏目录。安装完成后新开 Codex 聊天；若技能还未出现，重新打开 Codex App。插件不需要 MCP 服务，也不需要额外安装 Python 包。

以上“一键”负责**拉取和安装插件**；首次使用的服务器登录、linger 和 SSH 配置需按下面步骤准备一次。

## 2. 首次准备 Linux 服务器

以下命令在**始终开机的 Linux 服务器**执行，使用与后续 SSH 相同的用户。若服务器已准备好，可直接检查条件并跳过已完成的操作。

```bash
python3 --version
codex --version
codex login status
```

服务器尚未安装 Codex CLI 时，在已有 Node.js/npm 的环境执行：

```bash
npm install -g @openai/codex
```

尚未登录时执行设备登录，按终端给出的地址与验证码完成操作：

```bash
codex login --device-auth
codex login status
```

然后检查 user manager 和 linger：

```bash
systemctl --user show-environment >/dev/null
loginctl show-user "$(id -u)" --property=Linger --value
```

第二条命令需要输出 `yes`。如果输出 `no`，请由有管理员权限的人在服务器上执行：

```bash
sudo loginctl enable-linger "$(id -un)"
```

不要用 sudo 运行 Codex 或插件任务；linger 应开启在实际执行任务的普通用户上。`systemctl --user` 连接失败时，需先配置该用户的 systemd user manager；没有 systemd 的服务器或容器不能直接使用本插件当前的托管方式。

## 3. 配置 SSH 并检查环境

### Codex 工具已经在服务器执行

无需再配置一层 SSH。在插件源码目录运行：

```bash
python3 skills/server-tasks/scripts/client.py --local-server doctor
```

### Codex 工具在本地电脑执行

先在本地 `~/.ssh/config`（Windows 为用户目录下的 `.ssh/config`）配置自己的服务器，例如：

```sshconfig
Host my-server
    HostName YOUR_SERVER_IP
    User YOUR_SERVER_USER
    IdentityFile ~/.ssh/id_ed25519
```

替换服务器地址和用户名，并使用已有可用密钥。先手动连接一次，完成服务器主机密钥校验，再确认后续可以免交互连接：

```bash
ssh my-server
```

从服务器执行 `exit` 返回本地后，在插件源码目录运行：

```bash
ssh -T -o BatchMode=yes my-server 'python3 --version'
python3 skills/server-tasks/scripts/client.py --host my-server doctor
```

Windows PowerShell 中，若 Python 命令是 `python`，将本地调用中的 `python3` 改为 `python`；远端命令仍按服务器环境使用 `python3`。带口令的密钥需要预先加载到 SSH agent，插件不会弹出口令输入窗口。

`doctor` 返回 `"ready": true` 表示运行条件通过，配合前面的服务器登录检查后即可提交任务。`doctor` 不调用模型，也不检查登录凭据是否有效。若远端非交互 PATH 找不到 Codex，先在服务器运行 `command -v codex` 得到绝对路径，再指定：

```bash
python3 skills/server-tasks/scripts/client.py --host my-server \
  --codex-bin /实际路径/codex doctor
```

把主机别名和需要的 Codex 路径告诉当前聊天，后续操作沿用同一配置。SSH 模式会自动缓存运行器到服务器，无需在服务器另装桌面应用或手动克隆此插件。

## 4. 在 Codex 中使用

新开聊天，先选择实际执行方式进行检查：

```text
$server-tasks 使用 SSH 主机 my-server 检查服务器能否后台续跑。
```

如果工具已经在目标服务器运行，改为：

```text
$server-tasks 使用当前服务器检查后台续跑条件。
```

确认检查通过后，提交具体任务；工作目录必须是服务器上真实存在的目录：

```text
$server-tasks 使用 SSH 主机 my-server，在 /path/to/your/repository 后台执行：阅读项目结构并总结入口，不修改文件。返回任务编号。
```

拿到 `job_id` 后，客户端可以断网、退出或关机，服务器继续执行。重新打开 Codex 后，用同一服务器查询或继续：

```text
$server-tasks 使用 SSH 主机 my-server 查看最近任务的进度和结果。
$server-tasks 查看 SSH 主机 my-server 上任务 <job_id> 的结果。
$server-tasks 继续 SSH 主机 my-server 上的任务 <job_id>：进一步分析测试入口。
$server-tasks 停止 SSH 主机 my-server 上的任务 <job_id>。
```

已在服务器执行工具时，把上述“SSH 主机 my-server”替换为“当前服务器”。同一任务始终使用相同的主机和执行用户；自定义了 `--state-dir` 时还需使用相同状态目录。

执行中的任务先查询；原任务结束或明确停止后才使用“继续”。任务上下文来自提交的提示词，请写清目标、允许修改的范围、项目约束和完成标准。

## 运行条件

- 执行服务器：Linux、Python 3.10+、已安装并登录的 Codex CLI。
- `systemd-run --user` 可用，并启用用户 linger，保证退出 SSH 登录后 user manager 仍运行。
- 客户端：Python 3.10+；SSH 模式还需要 OpenSSH。Windows 客户端使用 SSH 模式，服务器仍须是 Linux。
- `doctor` 只检查条件，不会自动开启 linger、安装依赖或修改系统登录配置。
- 服务器须保持开机联网。服务器重启会中断任务，插件不会自动重放结果不明的写操作；检查后可以显式继续。

服务器未配置 linger 时，请由服务器管理员按自己的管理方式配置。插件不会调用 sudo 或调整已有服务。

## 命令行

以下命令从本仓库根目录运行。`--cwd` 必须是服务器上真实存在的绝对目录。

```bash
python3 skills/server-tasks/scripts/client.py --local-server doctor
python3 skills/server-tasks/scripts/client.py --local-server submit \
  --cwd /path/to/your/repository --sandbox read-only \
  --request-id example-analysis-001 --prompt-file - <<'TASK'
阅读项目结构并总结代码入口，不修改文件。
TASK
python3 skills/server-tasks/scripts/client.py --local-server list
python3 skills/server-tasks/scripts/client.py --local-server status <job_id>
python3 skills/server-tasks/scripts/client.py --local-server events <job_id> --after-seq 0
python3 skills/server-tasks/scripts/client.py --local-server resume <job_id> \
  --request-id example-analysis-002 --prompt '继续分析测试入口。'
python3 skills/server-tasks/scripts/client.py --local-server cancel <job_id>
```

远程操作示例：

```bash
python3 skills/server-tasks/scripts/client.py --host my-server doctor
python3 skills/server-tasks/scripts/client.py --host my-server \
  --codex-bin /path/to/codex submit --cwd /path/to/your/repository \
  --request-id example-remote-001 --prompt '总结项目结构，不修改文件。' --sandbox read-only
```

`--codex-bin` 可在远程非交互 shell 的 PATH 找不到 Codex 时指定其绝对路径。

## 执行与恢复

1. 客户端把请求交给服务器，返回 `job_id`。每个任务由独立 systemd user service 执行。
2. SQLite 保存任务状态和带游标的事件；原始输出和最终结果保留在服务器。
3. 桌面退出后 service 继续运行。回来用 `status` 查看结果，`events` 按 `next_seq` 增量读取。
4. `resume` 创建新任务，并通过已保存的原生 Codex `thread_id` 继续服务器会话。

`request-id` 用于提交去重。网络结果不明时复用相同编号和内容，不换编号重复提交。`queued` 只表示已接收；只有 `completed` 表示本轮正常结束。执行失败、进程消失或取消不会被报告为成功。同一 Codex 会话不能同时恢复多次。

默认沙箱为 `workspace-write`，纯分析可用 `read-only`。后台任务不能弹出桌面授权窗口；超出沙箱的操作会失败，不会自动绕过权限。停止任务只停止对应的插件 service，保留代码目录及其他会话。

## 数据位置

状态默认存放在服务器的 `$XDG_STATE_HOME/codex-server-tasks`，未设置时为 `~/.local/state/codex-server-tasks`。`--state-dir` 可指定另一个私有绝对目录。

状态包含提示词、事件和输出。启动期间会短暂保存权限为 `0600` 的环境快照，worker 读取后删除；任务目录和数据库限制为当前用户访问。不要把运行状态目录、环境文件、Codex 登录目录或 SSH 密钥加入开源仓库。本仓库仅包含插件源码、文档、许可证和隔离测试。

## 测试

无需下载依赖，不调用真实模型：

```bash
python3 tests/test_server_tasks.py -v
```

9 项隔离测试覆盖提交进程退出、请求去重、失败判定、会话恢复、跨任务事件分页、定向停止和 SSH 参数隔离。测试使用临时 fake Codex 与 fake systemd，运行后清理。

开发时另做过真实 systemd + Codex CLI 的只读检查：提交命令退出后得到预期标记，再从同一 `thread_id` 恢复并得到续跑标记。未做真实 SSH 断网或关闭桌面 App 的端到端测试。

## 许可证

[MIT](LICENSE)。这是独立社区插件，与 OpenAI 官方产品无隶属关系。

官方参考：[插件打包](https://developers.openai.com/plugins/build/plugins)、[Codex CLI](https://learn.chatgpt.com/docs/cli/reference)。
