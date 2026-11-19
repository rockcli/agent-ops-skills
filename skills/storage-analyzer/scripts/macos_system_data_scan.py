#!/usr/bin/env python3
"""Read-only evidence collector for macOS System Data investigations."""

import json
import os
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")


def run(command, timeout=30):
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"timeout after {timeout}s", "returncode": None}
    except Exception as exc:
        return {"ok": False, "stdout": "", "stderr": str(exc), "returncode": None}


def size_bytes(path, timeout=30):
    if not path or not os.path.exists(path):
        return None
    result = run(["du", "-x", "-sk", path], timeout=timeout)
    if not result["ok"] or not result["stdout"]:
        return None
    try:
        return int(result["stdout"].split()[0]) * 1024
    except (ValueError, IndexError):
        return None


def path_row(path, timeout=30):
    value = size_bytes(path, timeout=timeout)
    return {"path": path, "size_bytes": value, "exists": os.path.exists(path)}


def data_volume():
    result = run(["df", "-k", "/System/Volumes/Data"])
    row = {"path": "/System/Volumes/Data", "raw": result["stdout"]}
    lines = result["stdout"].splitlines()
    if len(lines) >= 2:
        fields = lines[-1].split()
        if len(fields) >= 6:
            row.update({
                "total_bytes": int(fields[1]) * 1024,
                "used_bytes": int(fields[2]) * 1024,
                "available_bytes": int(fields[3]) * 1024,
                "capacity": fields[4],
            })
    return row


def current_user_dirs():
    temp = run(["getconf", "DARWIN_USER_TEMP_DIR"])["stdout"].rstrip("/")
    cache = run(["getconf", "DARWIN_USER_CACHE_DIR"])["stdout"].rstrip("/")
    base = os.path.dirname(temp) if temp else ""
    dirs = {"base": base, "T": temp, "C": cache, "X": os.path.join(base, "X") if base else ""}
    rows = []
    for kind in ("X", "T", "C"):
        path = dirs[kind]
        rows.append({"kind": kind, **path_row(path, timeout=45)})

    candidates = []
    patterns = ("code_sign_clone", "pytest-of-", "playwright_chromiumdev_profile-")
    for kind in ("X", "T"):
        root = dirs[kind]
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not any(pattern in name for pattern in patterns):
                continue
            path = os.path.join(root, name)
            item = {"kind": kind, "name": name, **path_row(path, timeout=45)}
            if shutil.which("lsof"):
                opened = run(["lsof", "+D", path], timeout=20)
                item["open_files"] = opened["stdout"].splitlines()[1:21] if opened["stdout"] else []
                item["lsof_error"] = opened["stderr"] if not opened["ok"] else ""
            candidates.append(item)
    return {"directories": rows, "candidates": candidates}


def users_inventory():
    accounts = []
    result = run(["dscl", ".", "-list", "/Users", "UniqueID"])
    for line in result["stdout"].splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].isdigit() and int(fields[1]) >= 500:
            name = fields[0]
            details = run(["dscl", ".", "-read", f"/Users/{name}", "NFSHomeDirectory", "UserShell"])
            home = ""
            shell = ""
            for detail in details["stdout"].splitlines():
                if detail.startswith("NFSHomeDirectory:"):
                    home = detail.split(":", 1)[1].strip()
                elif detail.startswith("UserShell:"):
                    shell = detail.split(":", 1)[1].strip()
            accounts.append({
                "name": name, "uid": int(fields[1]), "home": home, "shell": shell,
                "human_candidate": home.startswith("/Users/") and shell not in ("", "/usr/bin/false"),
            })
    homes = []
    try:
        for name in sorted(os.listdir("/Users")):
            path = os.path.join("/Users", name)
            if os.path.isdir(path):
                homes.append({"name": name, "path": path})
    except OSError:
        pass
    return {"accounts": accounts, "home_directories": homes, "logged_in": run(["who"])["stdout"].splitlines()}


def find_simctl():
    if shutil.which("xcrun"):
        found = run(["xcrun", "--find", "simctl"])
        if found["ok"] and os.path.isfile(found["stdout"]):
            return found["stdout"]
    embedded = "/Library/Developer/PrivateFrameworks/CoreSimulator.framework/Versions/A/Resources/bin/simctl"
    return embedded if os.path.isfile(embedded) else None


def simulator_inventory():
    selected = run(["xcode-select", "-p"])["stdout"]
    simctl = find_simctl()
    runtime = run([simctl, "runtime", "list", "-j"], timeout=30) if simctl else None
    parsed = None
    if runtime and runtime["ok"]:
        try:
            parsed = json.loads(runtime["stdout"])
        except json.JSONDecodeError:
            parsed = None
    roots = [
        "/Library/Developer/CoreSimulator/Images",
        "/Library/Developer/CoreSimulator/Caches",
        "/Library/Developer/CoreSimulator/Profiles",
        os.path.join(HOME, "Library/Developer/CoreSimulator"),
    ]
    return {
        "developer_dir": selected,
        "xcode_app_exists": os.path.exists("/Applications/Xcode.app"),
        "simctl": simctl,
        "runtimes": parsed,
        "runtime_text": runtime["stdout"] if runtime else "",
        "paths": [path_row(path, timeout=45) for path in roots],
        "mounts": [line for line in run(["mount"])["stdout"].splitlines() if "CoreSimulator" in line],
    }


def main():
    if sys.platform != "darwin":
        print(json.dumps({"supported": False, "error": "macOS only"}))
        return 2
    started = time.time()
    data = {
        "supported": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data_volume": data_volume(),
        "apfs_snapshots": run(["diskutil", "apfs", "listSnapshots", "/"], timeout=30),
        "users": users_inventory(),
        "user_temp": current_user_dirs(),
        "system_data_roots": [path_row(path, timeout=45) for path in (
            "/private/var/folders",
            "/private/var/db/diagnostics",
            "/private/var/db/uuidtext",
            "/private/var/vm",
            "/Library/Developer/CoreSimulator",
            os.path.join(HOME, "Library/Containers/com.docker.docker"),
        )],
        "core_simulator": simulator_inventory(),
    }
    data["scan_seconds"] = round(time.time() - started, 1)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
