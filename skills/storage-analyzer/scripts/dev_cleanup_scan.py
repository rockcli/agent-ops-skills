#!/usr/bin/env python3
"""Read-only inventory for dependency caches, project envs, and Docker.

The script never prunes, pulls, removes, or changes package/Docker state. It
emits evidence for an agent to classify; cleanup commands are suggestions only.
"""
import json
import http.client
import os
import shutil
import socket
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

HOME = os.path.expanduser("~")


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "stdout": p.stdout.strip(),
                "stderr": p.stderr.strip(), "returncode": p.returncode}
    except Exception as exc:
        return {"ok": False, "stdout": "", "stderr": str(exc), "returncode": None}


def size_bytes(path):
    if not path or not os.path.exists(path):
        return 0
    r = run(["du", "-sk", path], timeout=120)
    if not r["ok"] or not r["stdout"]:
        return 0
    try:
        return int(r["stdout"].split()[0]) * 1024
    except (ValueError, IndexError):
        return 0


def human_bytes(value):
    n = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit not in ("B", "KB") else f"{int(n)} {unit}"
        n /= 1024


def lock_holders(lock_path):
    """Return bounded process evidence for a cache lock without changing it."""
    if not shutil.which("lsof") or not os.path.exists(lock_path):
        return []
    result = run(["lsof", lock_path], timeout=15)
    lines = result["stdout"].splitlines()
    holders = []
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 3 or not fields[1].isdigit():
            continue
        pid = int(fields[1])
        process = run(["ps", "-p", str(pid), "-o", "pid=,ppid=,etime=,command="], timeout=5)
        holders.append({
            "command": fields[0], "pid": pid, "user": fields[2],
            "process": process["stdout"],
        })
    return holders[:50]


def resolve_path(command, fallback=None):
    if not shutil.which(command[0]):
        return None
    r = run(command)
    if r["ok"] and r["stdout"]:
        return os.path.realpath(os.path.expanduser(r["stdout"].splitlines()[-1].strip()))
    return os.path.realpath(os.path.expanduser(fallback)) if fallback else None


def package_caches():
    specs = [
        ("homebrew", ["brew", "--cache"], "~/Library/Caches/Homebrew",
         "brew cleanup --dry-run", "brew cleanup", "green"),
        ("npm", ["npm", "config", "get", "cache"], "~/.npm",
         "npm cache verify", "npm cache clean --force", "yellow"),
        ("pnpm", ["pnpm", "store", "path"], "~/.pnpm-store",
         "pnpm store status", "pnpm store prune", "green"),
        ("uv", ["uv", "cache", "dir"], "~/.cache/uv",
         "uv cache dir", "uv cache prune", "green"),
        ("pip", ["pip3", "cache", "dir"], "~/Library/Caches/pip",
         "pip3 cache info", "pip3 cache purge", "green"),
    ]
    rows = []
    for manager, command, fallback, inspect_cmd, cleanup_cmd, tier in specs:
        path = resolve_path(command, fallback)
        if not path:
            continue
        if manager == "npm":
            path = os.path.join(path, "_cacache")
        total = size_bytes(path)
        row = {
            "manager": manager, "path": path, "size_bytes": total,
            "size_h": human_bytes(total), "suggested_tier": tier,
            "inspect_command": inspect_cmd, "cleanup_command": cleanup_cmd,
            "note": ("npm clean requires --force; prefer verify unless space pressure justifies it."
                     if manager == "npm" else "Use the manager command, not file-level deletion."),
        }
        if manager == "uv":
            holders = lock_holders(os.path.join(path, ".lock"))
            row["lock_holders"] = holders
            row["lock_status"] = "held" if holders else "free"
            if holders:
                row["suggested_tier"] = "yellow"
                row["note"] = "uv cache lock is held; do not use --force. Stop or finish the owning services first."
        rows.append(row)

    for manager, path, inspect_cmd in (
        ("cargo-registry-cache", "~/.cargo/registry/cache", "cargo metadata --no-deps"),
        ("cargo-git-db", "~/.cargo/git/db", "cargo metadata --no-deps"),
        ("gradle-cache", "~/.gradle/caches", "gradle --version"),
        ("go-module-cache", "~/go/pkg/mod/cache", "go env GOMODCACHE"),
    ):
        expanded = os.path.realpath(os.path.expanduser(path))
        if not os.path.exists(expanded):
            continue
        total = size_bytes(expanded)
        rows.append({
            "manager": manager, "path": expanded, "size_bytes": total,
            "size_h": human_bytes(total), "suggested_tier": "yellow",
            "inspect_command": inspect_cmd, "cleanup_command": None,
            "note": "No broadly safe built-in prune was selected; review in project context.",
        })
    rows.sort(key=lambda item: item["size_bytes"], reverse=True)
    return rows


LOCKFILES = (
    "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb",
    "uv.lock", "poetry.lock", "Pipfile.lock", "requirements.txt",
)
DEPENDENCY_DIRS = {"node_modules", ".venv", "venv", ".tox", ".nox"}
SKIP_DIRS = {".git", ".hg", ".svn", "Library", "Pictures", "Movies"}


def project_dependencies(max_depth=5, limit=120):
    roots = [
        os.path.join(HOME, "Documents", "git_code"),
        os.path.join(HOME, "Documents", "private_codes"),
    ]
    found = []
    queue = deque((root, 0) for root in roots if os.path.isdir(root))
    while queue and len(found) < limit:
        current, depth = queue.popleft()
        try:
            entries = list(os.scandir(current))
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name in DEPENDENCY_DIRS:
                parent = current
                locks = [name for name in LOCKFILES if os.path.isfile(os.path.join(parent, name))]
                total = size_bytes(entry.path)
                found.append({
                    "name": entry.name, "path": entry.path, "project": parent,
                    "size_bytes": total, "size_h": human_bytes(total),
                    "lockfiles": locks, "rebuild_evidence": bool(locks),
                    "suggested_tier": ("red" if "/.ai/cezar/worktrees/" in entry.path
                                       or "/.codex/worktrees/" in entry.path else "yellow"),
                    "classification": ("protected_worktree_dependency"
                                       if "/.ai/cezar/worktrees/" in entry.path
                                       or "/.codex/worktrees/" in entry.path
                                       else "project_dependency_review"),
                    "note": ("Managed worktree dependency; use its controller lifecycle, not direct cleanup."
                             if "/.ai/cezar/worktrees/" in entry.path
                             or "/.codex/worktrees/" in entry.path
                             else "Project dependency environment; verify Git/worktree and active-process state before removal."),
                })
                continue
            if depth < max_depth and entry.name not in SKIP_DIRS:
                queue.append((entry.path, depth + 1))
    found.sort(key=lambda item: item["size_bytes"], reverse=True)
    return found


def json_lines(command, timeout=45):
    r = run(command, timeout=timeout)
    if not r["ok"]:
        return r, []
    rows = []
    for line in r["stdout"].splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return r, rows


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, unix_socket, timeout=15):
        super().__init__("localhost", timeout=timeout)
        self.unix_socket = unix_socket

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_socket)


def docker_engine_df():
    """Read Engine /system/df for unique/shared image sizes when available."""
    endpoint = run([
        "docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"
    ])
    if not endpoint["ok"] or not endpoint["stdout"]:
        return {}
    try:
        host = json.loads(endpoint["stdout"])
    except json.JSONDecodeError:
        host = endpoint["stdout"].strip('"')
    if not host.startswith("unix://"):
        return {}
    connection = UnixHTTPConnection(host[len("unix://"):])
    try:
        connection.request("GET", "/system/df")
        response = connection.getresponse()
        if response.status != 200:
            return {}
        return json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, http.client.HTTPException):
        return {}
    finally:
        connection.close()


def verify_registry_manifests(images, timeout=20):
    """Read-only, bounded registry checks for registry-named unused images."""
    checks = []
    targets = []
    for image in images:
        if image["classification"] != "unused_registry_name_unverified" or not image["tags"]:
            continue
        tag = next((t for t in image["tags"] if not t.startswith("registry-1.docker.io/")),
                   image["tags"][0])
        targets.append((image, tag))

    def check(target):
        image, tag = target
        try:
            result = subprocess.run(["docker", "manifest", "inspect", tag],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    text=True, timeout=timeout)
            status = "verified_remote" if result.returncode == 0 else "failed"
            detail = (result.stderr or "").strip()[:200]
        except subprocess.TimeoutExpired:
            status, detail = "timeout", f"exceeded {timeout}s"
        image["manifest_status"] = status
        image["manifest_tag"] = tag
        return {"tag": tag, "status": status, "detail": detail}

    with ThreadPoolExecutor(max_workers=4) as pool:
        checks.extend(pool.map(check, targets))
    return checks


def docker_inventory(check_manifests=False):
    if not shutil.which("docker"):
        return {"available": False, "reachable": False, "error": "docker CLI not found"}
    version = run(["docker", "version", "--format", "{{json .Server.Version}}"])
    if not version["ok"]:
        return {"available": True, "reachable": False, "error": version["stderr"]}

    _, summary = json_lines(["docker", "system", "df", "--format", "{{json .}}"])
    _, containers_raw = json_lines([
        "docker", "container", "ls", "-a", "--no-trunc", "--format", "{{json .}}"
    ])
    container_ids = [row.get("ID") for row in containers_raw if row.get("ID")]
    container_records = []
    if container_ids:
        inspected_containers = run(["docker", "container", "inspect", *container_ids], timeout=120)
        if inspected_containers["ok"]:
            try:
                container_records = json.loads(inspected_containers["stdout"])
            except json.JSONDecodeError:
                container_records = []
    raw_by_id = {row.get("ID"): row for row in containers_raw}
    containers = []
    for record in container_records:
        raw = raw_by_id.get(record.get("Id"), {})
        state = record.get("State") or {}
        containers.append({
            "id": record.get("Id"),
            "name": (record.get("Name") or "").lstrip("/") or raw.get("Names"),
            "image_id": record.get("Image"),
            "image_ref": (record.get("Config") or {}).get("Image") or raw.get("Image"),
            "state": state.get("Status") or raw.get("State"),
            "status": raw.get("Status"),
        })
    if not container_records:
        containers = [{
            "id": row.get("ID"), "name": row.get("Names"),
            "image_id": None, "image_ref": row.get("Image"),
            "state": row.get("State"), "status": row.get("Status"),
        } for row in containers_raw]

    engine_df = docker_engine_df()
    engine_images = {row.get("Id"): row for row in (engine_df.get("Images") or [])}

    image_ids_result = run(["docker", "image", "ls", "-aq", "--no-trunc"])
    image_ids = sorted(set(image_ids_result["stdout"].splitlines())) if image_ids_result["ok"] else []
    images = []
    if image_ids:
        inspected = run(["docker", "image", "inspect", *image_ids], timeout=120)
        try:
            records = json.loads(inspected["stdout"]) if inspected["ok"] else []
        except json.JSONDecodeError:
            records = []
        for record in records:
            image_id = record.get("Id")
            tags = record.get("RepoTags") or []
            digests = record.get("RepoDigests") or []
            users = [
                c["name"] for c in containers
                if c.get("image_id") == image_id or c.get("image_ref") in tags
            ]
            repos = [tag.rsplit(":", 1)[0] for tag in tags]
            common_official = {
                "alpine", "busybox", "debian", "mariadb", "mongo", "mysql",
                "nginx", "node", "postgres", "python", "rabbitmq", "redis", "ubuntu",
            }
            registry_named = any("/" in repo or repo in common_official for repo in repos)
            if users:
                classification = "protected_by_container"
            elif not tags:
                classification = "dangling_candidate"
            elif registry_named:
                classification = "unused_registry_name_unverified"
            else:
                classification = "unused_local_or_unverified"
            df_row = engine_images.get(image_id, {})
            shared_size = df_row.get("SharedSize")
            engine_size = df_row.get("Size")
            unique_size = (max(0, engine_size - shared_size)
                           if isinstance(engine_size, int) and isinstance(shared_size, int)
                           else None)
            first_repo = tags[0].rsplit(":", 1)[0] if tags else "<dangling>"
            images.append({
                "id": image_id, "tags": tags, "repo_digests": digests,
                "created": record.get("Created"), "size_bytes": record.get("Size", 0),
                "size_h": human_bytes(record.get("Size", 0)), "container_names": users,
                "project_group": first_repo,
                "shared_size_bytes": shared_size,
                "unique_size_bytes": unique_size,
                "unique_size_h": human_bytes(unique_size) if unique_size is not None else None,
                "engine_container_count": df_row.get("Containers"),
                "classification": classification,
                "manifest_check_commands": [f"docker manifest inspect {tag}" for tag in tags],
                "config_reference_check_commands": [
                    f"rg -n --fixed-strings {json.dumps(tag)} ~/Documents/git_code ~/Documents/private_codes"
                    for tag in tags
                ],
            })
    images.sort(key=lambda item: item["size_bytes"], reverse=True)
    manifest_checks = verify_registry_manifests(images) if check_manifests else []
    _, volumes_raw = json_lines(["docker", "volume", "ls", "--format", "{{json .}}"])
    volumes = [{"name": row.get("Name"), "driver": row.get("Driver"),
                "scope": row.get("Scope"), "classification": "protected_user_data"}
               for row in volumes_raw]
    return {
        "available": True, "reachable": True, "server_version": version["stdout"].strip('"'),
        "summary": summary, "images": images, "containers": containers, "volumes": volumes,
        "engine_df_available": bool(engine_df),
        "manifest_checks": manifest_checks,
        "commands": {
            "inspect": "docker system df -v",
            "dangling_images": "docker image prune",
            "unused_images": "docker image prune -a",
            "build_cache": "docker builder prune",
        },
        "policy": "Never prune containers or volumes automatically; never delete Docker.raw or ~/.docker.",
    }


def main(check_manifests=False):
    started = time.time()
    data = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "package_caches": package_caches(),
        "project_dependencies": project_dependencies(),
        "docker": docker_inventory(check_manifests=check_manifests),
    }
    data["scan_seconds"] = round(time.time() - started, 1)
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if sys.platform != "darwin":
        print(json.dumps({"error": "dev_cleanup_scan currently supports macOS only"}))
        sys.exit(2)
    unknown = [arg for arg in sys.argv[1:] if arg != "--check-docker-manifests"]
    if unknown:
        print(json.dumps({"error": "unknown arguments", "arguments": unknown}))
        sys.exit(2)
    main(check_manifests="--check-docker-manifests" in sys.argv[1:])
