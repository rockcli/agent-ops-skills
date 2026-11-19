---
name: colab-operator
description: Operate Google Colab environments (including A100/H100 GPUs and v5e/v6e TPUs) via the `colab` CLI (`google-colab-cli`). Use when asked to check Colab compute unit quota/usage, create or manage GPU/TPU sessions, run Python/shell on a remote Colab VM, sync files, automate environment setup (packages, auth, Drive), or export session history.
---

# Skill: Colab Session Operator

Operate Google Colab environments via the `colab` CLI (`google-colab-cli`): check Compute Unit balances, provision GPU/TPU sessions (`H100`, `A100`, `L4`, `G4`, `T4`, `v6e1`, `v5e1`), run Python/shell on the VM, sync files, and capture work as notebooks.

## Installation & Environment

- Install the `colab` CLI (`google-colab-cli`) via `uv tool install google-colab-cli` (typically installed to `$HOME/.local/bin/colab`). Ensure `$HOME/.local/bin` is in `PATH`.
- **Account Entitlements**: Depending on your Google Colab plan, available accelerators may include:
  - **GPUs**: `H100`, `A100`, `G4`, `L4`, `T4` (supports `--high-mem` on `A100`, `H100`, `T4`, CPU)
  - **TPUs**: `v6e1`, `v5e1`

## When to activate
- Checking Google Colab Compute Units (CU) balance or active GPU burn rate (`colab usage`).
- Creating or managing TPU/GPU sessions (especially A100 / H100).
- Running Python scripts, notebooks, or shell commands on a remote Colab VM.
- Syncing files between local and remote.
- Automating environment setup (packages, auth, Drive).
- Exporting session history as a Jupyter notebook.

## Mental model (read this first)
- **A session == a live Jupyter kernel on a rented VM.** `colab new` allocates a billable VM; `colab stop` releases it. Sessions stay alive as long as the kernel is active, with the Colab backend automatically maintaining liveness.
- **Kernel state PERSISTS across `colab exec` / `colab repl` calls in the same session.** Each invocation reattaches to the *same* kernel (the kernel ID is cached in local state) and only closes the websocket on exit — it does **not** shut the kernel down. So imports, variables, and defined functions survive between separate `colab exec` commands. Build up state incrementally; don't re-import everything each call. (`colab stop` and `colab restart-kernel` are what actually reset it.)
- **Default working directory is `/content`.** Every `exec`/`repl`/`run` `cd`s there first; prefer absolute paths (`/content/...`) for file work. For `colab ls/rm/upload/download`, absolute `/content/...` paths work and the default `ls` path is `content` (VM root).
- **`colab` is fire-and-forget.** Each command authenticates, does one thing, and exits.

## Authentication
- The global flag is `--auth={oauth2,adc}` and the **default is `oauth2`**.
- **Pre-configured OAuth2**: OAuth2 credentials (including an auto-refreshing `refresh_token`) are already cached at `~/.config/colab-cli/token.json` using the bundled client config. Normal commands (`colab usage`, `colab sessions`, `colab new`, `colab run`, etc.) work out of the box without extra flags.
- **Verify auth & quota in one shot**:
  - `colab usage` — prints current Compute Unit balance, hourly usage rate, and active assignments count.
  - `colab sessions` — read-only, lists active server assignments.
  - `colab whoami` — hidden debug command: prints the active email, scopes, audience, and expiry.
- **If re-authentication is ever needed**:
  - `oauth2` uses a remote copy-paste flow (`https://sdk.cloud.google.com/applicationdefaultauthcode.html`), which prints a URL and prompts for an authorization code on stdin.
  - Alternatively, for `adc`:
    ```bash
    gcloud auth application-default login \
      --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
    ```
- **Do NOT confuse `colab auth` with CLI authentication.** `colab auth` injects *VM-side* GCP credentials into the running kernel (so notebook code can call BigQuery/GCS); it is orthogonal to how the CLI itself authenticates.

## Workflow

### Check Quota & Usage
- `colab usage` shows remaining Compute Units (`currentBalance`), active burn rate (`Usage rate: X.XX/hr`), and `Active assignments`.

### Provision
- `colab new -s <name>` (CPU). Add `--gpu A100` (or `H100`, `L4`, `G4`, `T4`) or `--tpu v6e1` (or `v5e1`) for accelerators. Add `--high-mem` for high-RAM machine shapes when needed.
- **Always pass `-s <name>`** — an omitted name is auto-generated as a random 6-hex string, which makes later commands ambiguous.
- Supported `--gpu`: `T4`, `L4`, `G4`, `H100`, `A100`. Supported `--tpu`: `v5e1`, `v6e1`.
- **Gotcha**: an unrecognized `--gpu` value silently falls back to **A100**. A `400` on `colab new` with an accelerator means no quota/entitlement or regional stock out for it — fall back to `--gpu A100`, `--gpu L4`, or `--gpu T4`.

### Execute
- **Preferred**: `colab exec -s <name> -f <script.py>` runs a local script on the remote VM (read locally, sent to the kernel — no manual upload needed).
- **Piped code**: `echo "print(1)" | colab exec -s <name>` or `cat script.py | colab exec -s <name>`.
- **Notebooks**: `colab exec -s <name> -f nb.ipynb` runs each code cell and writes results to `<basename>_output.ipynb` next to the input. A `# @title Foo` first line labels the cell in progress output.
- **Plots/images**: PNG/JPEG outputs are intercepted. Use `--output-image <path>` on `exec`/`repl` to save to a known location (otherwise a temp path is printed). Inline terminal-image escapes are auto-suppressed when stdout isn't a TTY, so piped/captured output stays clean.
- **Shell**: `echo "cmd" | colab console -s <name>` for batch shell. Console wraps bash in tmux, so even piped output contains terminal-control bytes — filter with `grep -a` for a specific line. `exec` is faster when you don't need a real shell.
- **SSH**: `colab ssh -s <name>` opens an interactive SSH shell over WebSocket (or use `--proxy-mode` as an OpenSSH `ProxyCommand`).
- **Never run `colab repl`, `colab console`, `colab auth`, or `colab drivemount` interactively from an agent** — they expect a TTY and will hang. `repl`/`console` accept piped stdin and exit on EOF; `auth`/`drivemount` genuinely require a human at the terminal.

### Ephemeral one-shot jobs (`colab run`)
- `colab run [--gpu A100] [--high-mem] [--tpu v6e1] [--keep] [-s NAME] script.py [args...]` = `new` + `exec` + `stop` in one command. It provisions a fresh VM, runs the script with `sys.argv` and `__name__ == "__main__"` set like native `python script.py args`, then tears the VM down automatically (unless `--keep`).
- **Exit codes propagate**: an uncaught exception or `sys.exit(N)` in the script makes `colab run` exit non-zero.
- **Stream separation**: `colab run` writes its own `[colab] ...` chatter to **stderr** and the script's output to **stdout** — so `colab run job.py > out.txt` captures only the script's stdout. (`colab exec` streams the script's stdout/stderr live to your stdout/stderr.)
- Works as a shebang: `#!/usr/bin/env -S colab run --gpu A100` makes a `chmod +x`'d `.py` a self-contained "rent a GPU, run, clean up" script.
- A nonexistent script path exits non-zero **before** allocating a VM (no wasted compute).

### File Operations & Automation
- `colab ls [-s <name>] [PATH]` — list files on VM (default `/content`).
- `colab upload [-s <name>] LOCAL REMOTE` — upload local file to VM.
- `colab download [-s <name>] REMOTE LOCAL` — download remote file from VM to local.
- `colab rm [-s <name>] PATH` — remove remote file.
- `colab install -s <name> pkg1 pkg2` — installs via `uv pip install --system`, falling back to `pip`. Also `colab install -s <name> -r requirements.txt`.
- `colab auth -s <name>` — VM-side GCP creds, needed before in-VM GCS/BigQuery calls (interactive; not agent-runnable).
- `colab drivemount -s <name> [PATH]` — mounts Drive at `/content/drive` by default (interactive; not agent-runnable).

### Inspect & report
- `colab help` (or `colab help <cmd>`) lists/explains commands; the listing is alphabetical.
- `colab sessions` lists server-side assignments and auto-prunes stale local entries. Orphans with no local record show as `[?]`.
- `colab status [-s <name>]` shows hardware, IDLE/BUSY, and last execution.
- `colab log -s <name> [-n 20] [-t TYPE]` shows recent structured events; invaluable when a task fails.
- `colab log -s <name> -o summary.ipynb` exports the session as a notebook (also `.md`, `.txt`, `.jsonl` by suffix).
- `colab url -s <name>` prints a browser URL that attaches the Colab web UI to your existing CLI session instead of allocating a new VM (add `--open` to launch it).

## Safety
- **Always `colab stop -s <name>` when done** — idle VMs burn compute units (~10–15 CU/hr for A100, ~18 CU/hr for H100). `colab run` (without `--keep`) self-cleans even if the script errors.
- Local state lives in `~/.config/colab-cli/sessions.json` (settings in `settings.json`, history in `history/*.jsonl`). Don't edit by hand.
- **Isolate parallel/agent runs** with the global `--config <path>` flag to point session state at a scratch file (e.g. `colab --config /tmp/agent.json new -s job`).

## Recovery
- "Session not found" / 404 / 401 on exec: the backend pruned the VM. `colab exec`/`repl` detect this and clean up local state automatically — run `colab sessions` and re-create with `colab new`.
- Execution timeout or wedged kernel: `colab restart-kernel -s <name>` (keeps the VM, resets the kernel), or `colab stop` then `colab new`.
