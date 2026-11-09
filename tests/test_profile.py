"""Exercise profile shell functions with command doubles; no Proxmox required."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
core = (ROOT / "core.sh").read_text(encoding="utf-8")


def function(name):
    start = core.index(name + "() {")
    return core[start:core.index("\n}", start) + 2]


def shell(body, env=None):
    settings = dict(os.environ)
    if env:
        settings.update(env)
    return subprocess.run([BASH, "-s"], input=body, text=True, encoding="utf-8",
                          capture_output=True, env=settings, timeout=15)


allocator = r"""
pvesh() { [ "${API_FAIL:-0}" = 0 ] || return 1; echo '[]'; }
jq() { cat >/dev/null; [ "${JSON_FAIL:-0}" = 0 ] || return 1; printf '%s' "$USED"; }
""" + function("get_next_free_hundred") + "\nget_next_free_hundred\n"
for used, expected in (("", "20000"), ("10101\n10102", "20000"),
                       ("20002", "20100"), ("20002\n20199", "20200")):
    result = shell(allocator, {"USED": used})
    assert result.returncode == 0 and result.stdout.strip() == expected, result
for setting in ("API_FAIL", "JSON_FAIL"):
    result = shell(allocator, {"USED": "", setting: "1"})
    assert result.returncode != 0 and not result.stdout.strip(), result
print("PASS: allocator starts at 20000, skips occupied blocks, stops on inventory failures")

mocks = r"""
role_label() { echo "$1"; }
find_image() { echo "$ARCHIVE"; }
zstdcat() { printf 'mock vma data'; return "${ZSTD_CODE:-0}"; }
qmrestore() { cat >/dev/null; printf 'qmrestore'; printf ' <%s>' "$@"; printf '\n'; return "${RESTORE_CODE:-0}"; }
qm() { { printf 'qm'; printf ' <%s>' "$@"; printf '\n'; } >> "$TRACE"; }
pvesh() { :; }
iso_spec() { :; }
patch_net() { :; }
"""
# qmrestore stdout is captured by restore_vm, so record its argv in a separate trace.
mocks = mocks.replace("printf 'qmrestore';", "printf 'qmrestore' >> \"$TRACE\";").replace(
    "printf ' <%s>' \"$@\"; printf '\\n'; return", "printf ' <%s>' \"$@\" >> \"$TRACE\"; printf '\\n' >> \"$TRACE\"; return")
with tempfile.TemporaryDirectory(prefix="demo-profile-") as directory:
    base = Path(directory)
    archive = base / "ISP.vma.zst"
    archive.write_bytes(b"fixture")
    def unix(path):
        return ("/" + path.drive[0].lower() + path.as_posix()[2:]) if os.name == "nt" else path.as_posix()
    trace = base / "trace"
    env = {"ARCHIVE": unix(archive), "TRACE": unix(trace), "TARGET_STORAGE": "ssd", "MOD_TYPE": "DEMO_2"}
    body = mocks + function("restore_vm") + '\nrestore_vm 20001 ISP "" DEMO_2_S1 demo_2\n'
    result = shell(body, env)
    assert result.returncode == 0, result.stderr
    calls = trace.read_text()
    assert "<--format>" not in calls and "<--force>" not in calls, calls
    assert "<snapshot>" not in calls and "<unlock>" not in calls, calls
    for option in ("ZSTD_CODE", "RESTORE_CODE"):
        trace.write_text("")
        result = shell(body, dict(env, **{option: "2"}))
        assert result.returncode == 2, (option, result)
        assert "qm <set>" not in trace.read_text(), trace.read_text()
print("PASS: restore uses supported native options, avoids force/snapshot/unlock and propagates both pipeline failures")

manifest = json.loads((ROOT / "UPSTREAM.json").read_text())
for filename, hashes in manifest["files"].items():
    assert hashlib.sha256((ROOT / filename).read_bytes()).hexdigest() == hashes["profile_sha256"]
    assert hashlib.sha256((ROOT / "vendor/upstream" / filename).read_bytes()).hexdigest() == hashes["upstream_sha256"]
with tempfile.TemporaryDirectory(prefix="demo-patch-") as directory:
    shutil.copy2(ROOT / "vendor/upstream/core.sh", Path(directory) / "core.sh")
    patch = (ROOT / "patches/0001-demo2-profile.patch").read_text(encoding="utf-8")
    result = subprocess.run([BASH, "-c", "patch -p1"], cwd=directory, input=patch,
                            text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr + result.stdout
    assert (Path(directory) / "core.sh").read_bytes() == (ROOT / "core.sh").read_bytes()
print("PASS: upstream/profile hashes and patch reproduce the distributed core.sh")
