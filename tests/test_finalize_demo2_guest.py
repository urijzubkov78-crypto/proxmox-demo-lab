import importlib.util
import os
import shutil
import shlex
import subprocess
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("finish",ROOT/"finalize_demo2.py")
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
BASH=shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
ROOTTEST=ROOT/"tmp"/("demo2-guest-tests-"+uuid.uuid4().hex[:8])
assert ROOTTEST.resolve().is_relative_to((ROOT/"tmp").resolve())
ROOTTEST.mkdir(parents=True)
MOCKS=r'''
id() { [ "$1" = -u ] && echo 0; }
blkid() {
    [ ! -e "$spz_test_root/no-iso" ] || return 2
    case "$1" in
        -L) [ "$2" = Additional ] || return 2; echo "$spz_test_root/dev/sr1";;
        -s) [ "$2" = TYPE ] && [ "$5" = "$spz_test_root/dev/sr1" ] || return 2; echo iso9660;;
        *) return 2;;
    esac
}
findmnt() {
    [ -e "$spz_test_root/mounted" ] || return 1
    fmt=
    while [ "$#" -gt 0 ]; do
        if [ "$1" = -o ]; then fmt=$2; shift; fi
        shift
    done
    case "$fmt" in
        SOURCE) echo "$spz_test_root/dev/sr1";;
        FSTYPE) echo iso9660;;
        OPTIONS) echo ro,relatime;;
        '') :;;
        *) return 2;;
    esac
}
mount() {
    [ "$1" = "$spz_test_root/mnt/additional" ] || return 2
    echo mount >> "$spz_test_root/calls"
    [ ! -e "$spz_test_root/mount-fails" ] || return 1
    : > "$spz_test_root/mounted"
}
apt-get() {
    [ "$1" = clean ] || return 2
    echo apt-clean >> "$spz_test_root/calls"
    [ ! -e "$spz_test_root/apt-busy" ]
}
apt-config() {
    [ "$*" = 'shell SPZ_PROXY Acquire::http::Proxy' ] || return 2
    value=
    fragment="$spz_test_root/etc/apt/apt.conf.d/99spetsnaz-proxy.conf"
    if [ -f "$fragment" ]; then
        value=$(awk -F '"' '/Acquire::http::Proxy/ {print $2}' "$fragment")
    fi
    [ ! -e "$spz_test_root/wrong-proxy" ] || value=http://wrong:3142
    [ -n "$value" ] || return 0
    printf "SPZ_PROXY='%s'" "$value"
    [ ! -e "$spz_test_root/semicolon" ] || printf ';'
    printf '\n'
}
systemctl() { [ "$1" = daemon-reload ]; }
curl() {
    found=0
    while [ "$#" -gt 0 ]; do
        if [ "$1" = --proxy ]; then
            [ "$2" = "$PROXY" ] || return 2
            found=1
            shift
        fi
        shift
    done
    [ "$found" = 1 ] || return 2
    [ ! -e "$spz_test_root/cache-fails" ] || return 7
}
'''

def case(name):
    p=ROOTTEST/name
    p.mkdir()
    for d in ("etc/apt/apt.conf.d","var/lib/apt/lists/partial","mnt/additional","dev"):
        (p/d).mkdir(parents=True,exist_ok=True)
    (p/"etc/altlinux-release").write_text("ALT Linux")
    (p/"etc/fstab").write_text("# original\nUUID=root / ext4 defaults 0 1\n")
    (p/"dev/sr1").write_text("fake iso device")
    (p/"var/lib/apt/lists/old-index").write_text("old")
    (p/"var/lib/apt/lists/partial/old-download").write_text("old")
    return p

def execute(p,mode="apply",iso=True,success=True):
    assert p.resolve().is_relative_to((ROOT/"tmp").resolve())
    unix=("/"+p.drive[0].lower()+p.as_posix()[2:]) if os.name=="nt" else p.as_posix()
    body=m.GUEST
    for prefix in ("/etc/","/var/lib/","/mnt/"):
        body=body.replace(prefix,unix+prefix)
    script=("spz_test_root="+shlex.quote(unix)+"\nMODE="+mode+
            "\nWITH_ISO="+str(int(iso))+"\nPROXY="+shlex.quote(m.PROXY)+
            "\nRPM="+shlex.quote(m.RPM)+"\n"+MOCKS+"\n"+body)
    result=subprocess.run([BASH,"--noprofile","--norc","-s"],input=script,text=True,
                          capture_output=True,timeout=30)
    if success:
        assert result.returncode==0,(result.returncode,result.stdout,result.stderr)
    else:
        assert result.returncode!=0,"expected a guest-shell failure"
    return result

n=0
def passed(label):
    global n
    n+=1
    print("PASS:",label)

p=case("apply")
original=(p/"etc/fstab").read_text()
r=execute(p)
assert "APT_PROXY=OK" in r.stdout and "ISO_MOUNT=OK" in r.stdout
assert (p/"mounted").exists()
assert (p/"var/lib/spetsnaz-finalize/fstab.before").read_text()==original
assert "UUID=root / ext4 defaults 0 1" in (p/"etc/fstab").read_text()
assert (p/"etc/apt/apt.conf.d/99spetsnaz-proxy.conf").read_text()==f'Acquire::http::Proxy "{m.PROXY}";\n'
assert not (p/"var/lib/apt/lists/old-index").exists()
assert not (p/"var/lib/apt/lists/partial/old-download").exists()
assert (p/"var/lib/apt/lists/partial").is_dir()
passed("apply: fstab/backup/readonly ISO/proxy/old-list cleanup with partial retained")

r=execute(p,mode="verify")
assert "CACHE_DOWNLOAD=OK" in r.stdout and "GUEST_CHECK=OK" in r.stdout
passed("verify: mount, fstab, effective APT proxy and actual-download status")

execute(p)
assert (p/"etc/fstab").read_text().count("LABEL=Additional")==1
assert (p/"var/lib/spetsnaz-finalize/fstab.before").read_text()==original
passed("reapplying is idempotent and retains the original fstab backup")

(p/"semicolon").touch()
execute(p,mode="verify")
passed("APT shell output accepted with or without a trailing semicolon")

p=case("no_iso")
(p/"no-iso").touch()
original=(p/"etc/fstab").read_text()
r=execute(p,success=False)
assert "ISO label" in r.stderr
assert (p/"etc/fstab").read_text()==original
assert not (p/"etc/apt/apt.conf.d/99spetsnaz-proxy.conf").exists()
passed("missing ISO stops before writing guest settings")

p=case("fstab_conflict")
unix=("/"+p.drive[0].lower()+p.as_posix()[2:]) if os.name=="nt" else p.as_posix()
(p/"etc/fstab").write_text("UUID=root / ext4 defaults 0 1\n/dev/other "+unix+"/mnt/additional ext4 defaults 0 2\n")
original=(p/"etc/fstab").read_text()
r=execute(p,success=False)
assert "conflicting entry" in r.stderr
assert (p/"etc/fstab").read_text()==original
passed("conflicting fstab entry is preserved and rejected")

p=case("nonempty_mountpoint")
(p/"mnt/additional/keep.txt").write_text("keep")
r=execute(p,success=False)
assert "not empty" in r.stderr and (p/"mnt/additional/keep.txt").read_text()=="keep"
passed("existing data at the mountpoint is preserved")

p=case("automount_failed")
execute(p)
calls=(p/"calls").read_text()
(p/"mounted").unlink()
r=execute(p,mode="verify",success=False)
assert "automatically" in r.stderr and not (p/"mounted").exists()
assert (p/"calls").read_text()==calls
passed("post-boot verification fails rather than hiding a missing automount")

p=case("unreachable_cache")
execute(p)
(p/"cache-fails").touch()
r=execute(p,mode="verify")
assert "CACHE_DOWNLOAD=FAILED" in r.stdout and "GUEST_CHECK=OK" in r.stdout
passed("cache download failure reported independently of configuration success")

p=case("wrong_effective_proxy")
execute(p)
(p/"wrong-proxy").touch()
r=execute(p,mode="verify",success=False)
assert "effective APT proxy" in r.stderr
passed("overridden effective APT proxy is rejected")

p=case("isp_without_iso")
(p/"no-iso").touch()
original=(p/"etc/fstab").read_text()
r=execute(p,iso=False)
assert "ISO_MOUNT=SKIP" in r.stdout and (p/"etc/fstab").read_text()==original
r=execute(p,mode="verify",iso=False)
assert "CACHE_DOWNLOAD=OK" in r.stdout
passed("ISP/client apply APT only and do not require or change the ISO mount")

p=case("apt_busy")
(p/"apt-busy").touch()
r=execute(p,success=False)
assert "GUEST_CHECK=OK" not in r.stdout
passed("APT cleanup failure propagates as a guest-command failure")

p=case("regression_no_conf_extension")
original_guest=m.GUEST
m.GUEST=m.GUEST.replace("/etc/apt/apt.conf.d/99spetsnaz-proxy.conf","/etc/apt/apt.conf.d/99spetsnaz-proxy")
r=execute(p,iso=False,success=False)
assert "effective APT proxy" in r.stderr
assert (p/"etc/apt/apt.conf.d/99spetsnaz-proxy").is_file()
assert not (p/"etc/apt/apt.conf.d/99spetsnaz-proxy.conf").exists()
passed("regression reproduces empty effective proxy for ignored fragment without .conf")
m.GUEST=original_guest
r=execute(p,iso=False)
assert "APT_PROXY=OK" in r.stdout
passed("same guest succeeds after writing a fragment with .conf extension")
import ast
tree=ast.parse((ROOT/"repair_alt_apt.py").read_text(encoding="utf-8"))
probe=next(ast.literal_eval(n.value) for n in tree.body
           if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=="PROBE" for t in n.targets))
original_guest=m.GUEST
m.GUEST=probe
p=case("repair_probe")
(p/"etc/apt/apt.conf.d/99spetsnaz-proxy").write_text(f'Acquire::http::Proxy "{m.PROXY}";\n')
r=execute(p,iso=False)
assert "ALT_PROXY_CONF_LOADING=OK" in r.stdout
assert (p/"etc/apt/apt.conf.d/99spetsnaz-proxy.conf").is_file()
passed("repair probe confirms default APT loading after adding .conf")

p=case("repair_foreign_conf")
(p/"etc/apt/apt.conf.d/99spetsnaz-proxy").write_text(f'Acquire::http::Proxy "{m.PROXY}";\n')
foreign=p/"etc/apt/apt.conf.d/99spetsnaz-proxy.conf"
foreign.write_text('Acquire::http::Proxy "http://other:3142";\n')
r=execute(p,iso=False,success=False)
assert foreign.read_text()=='Acquire::http::Proxy "http://other:3142";\n'
assert "different .conf" in r.stderr
passed("repair probe preserves a conflicting .conf file")
m.GUEST=original_guest
print(f"ALL {n} GUEST SHELL TESTS PASSED; mocked filesystem: {ROOTTEST}")
