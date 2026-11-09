#!/usr/bin/env python3
"""Repair ALT APT fragment names, confirm on ISP 20001, then resume preparation."""
import datetime
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path("/root/proxmox-auto-deploy")
WORK = Path("/root/pre-it-spetsnaz/demo2-finalize")
OLD = "/etc/apt/apt.conf.d/99spetsnaz-proxy"
NEW = OLD + ".conf"
PROBE = r'''set -eu
export LC_ALL=C
die() { echo "ERROR: $*" >&2; exit 1; }
[ -f /etc/altlinux-release ] || die "ALT Linux was not detected"
old=/etc/apt/apt.conf.d/99spetsnaz-proxy
new=/etc/apt/apt.conf.d/99spetsnaz-proxy.conf
expected='Acquire::http::Proxy "http://10.0.55.12:3142";'
[ -f "$old" ] || die "original proxy fragment is missing"
[ "$(cat "$old")" = "$expected" ] || die "original proxy fragment differs"
if [ -L "$new" ]; then die "new proxy fragment is a symlink"; fi
if [ -e "$new" ]; then
    [ -f "$new" ] && [ "$(cat "$new")" = "$expected" ] ||
        die "a different .conf fragment already exists"
else
    cp -p "$old" "$new"
fi
effective=$(apt-config shell SPZ_PROXY Acquire::http::Proxy)
[ "$effective" = "SPZ_PROXY='http://10.0.55.12:3142'" ] ||
[ "$effective" = "SPZ_PROXY='http://10.0.55.12:3142';" ] ||
    die "APT still does not report the expected proxy; host scripts were not changed"
echo ALT_PROXY_CONF_LOADING=OK
'''

def checked(*args, input=None, timeout=60):
    result = subprocess.run(args, input=input, text=True, encoding="utf-8",
                            errors="replace", capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                           or "Command failed: " + " ".join(args))
    return result.stdout.strip()

def patched(content):
    # Preserve all other edits, including VMID base and disabled automatic Day0.
    return re.sub(re.escape(OLD) + r"(?![A-Za-z0-9_.-])", lambda _: NEW, content)

def main():
    if os.geteuid() != 0:
        raise RuntimeError("Run on Proxmox as root")
    if not WORK.is_dir():
        raise RuntimeError("The preparation directory is missing")
    os.umask(0o077)
    with (WORK / "run.lock").open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        changes = []
        for name in ("finalize_demo2.py", "core.sh"):
            path = REPO / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("Missing or unexpected file: " + str(path))
            content = path.read_text(encoding="utf-8")
            updated = patched(content)
            if NEW not in updated:
                raise RuntimeError("No expected proxy path found in " + name)
            if name.endswith(".py"):
                compile(updated, str(path), "exec")
                if "state_path = WORK" not in updated or "--workers" not in updated:
                    raise RuntimeError("Unexpected preparation-script version")
            else:
                checked("bash", "-n", input=updated)
            changes.append((path, content, updated))

        if checked("qm", "status", "20001") != "status: running":
            raise RuntimeError("VM 20001 must be running for the proxy check")
        checked("qm", "agent", "20001", "ping", timeout=20)
        response = json.loads(checked("qm", "guest", "exec", "20001",
                                     "--pass-stdin", "1", "--timeout", "30",
                                     "--", "/bin/sh", "-s", input=PROBE, timeout=50))
        response = response.get("result", response)
        if (not response.get("exited") or response.get("exitcode") != 0 or
                response.get("out-truncated") or response.get("err-truncated") or
                "ALT_PROXY_CONF_LOADING=OK" not in response.get("out-data", "").splitlines()):
            raise RuntimeError("Proxy check failed; host scripts were not changed.\n"
                               + response.get("err-data", "")
                               + response.get("out-data", ""))
        print("VM 20001: APT reads 99spetsnaz-proxy.conf and reports the expected proxy.", flush=True)

        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        for path, content, updated in changes:
            if content == updated:
                print(path.name + ": already repaired", flush=True)
                continue
            if path.read_text(encoding="utf-8") != content:
                raise RuntimeError("File changed while checking: " + str(path))
            backup = path.with_name(path.name + ".before-apt-conf-" + stamp)
            if backup.exists():
                raise RuntimeError("Backup already exists: " + str(backup))
            shutil.copy2(path, backup)
            temporary = path.with_name(path.name + ".apt-conf-new-" + stamp)
            with temporary.open("x", encoding="utf-8", newline="\n") as f:
                f.write(updated)
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary, path)
            print(path.name + ": repaired; backup " + str(backup), flush=True)

    print("Resuming DEMO_2 with two workers; existing state is retained.", flush=True)
    os.execv(sys.executable, [sys.executable, "-u", str(REPO / "finalize_demo2.py"),
                             "--workers", "2"])

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        sys.exit(1)
