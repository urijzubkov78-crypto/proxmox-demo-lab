#!/usr/bin/env python3
"""Finish the 15 existing DEMO_2 stands. Proxmox 7 / Python 3.9+, stdlib only.

APT behavior follows apt_job(cache), proxy branch, in core.sh v0.7.9:
99spetsnaz-proxy -> apt-get clean -> delete list files -> retain lists/partial.
No package updates, guest network changes, disk conversion or disk formatting.
"""
import argparse
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

REPO = Path("/root/proxmox-auto-deploy")
WORK = Path("/root/pre-it-spetsnaz/demo2-finalize")
NODE = "pve"
ISO = "local:iso/Additional.iso"
PROXY = "http://10.0.55.12:3142"
RPM = ("http://mirror.yandex.ru/altlinux/p10/branch/x86_64/RPMS.classic/"
       "apt-conf-branch-10.0-alt1.p10.1.x86_64.rpm")
ROLES = {1: "ISP", 2: "HQ-RTR", 3: "BR-RTR", 4: "BR-FW",
         5: "HQ-SRV", 6: "HQ-CLI", 7: "BR-SRV"}
SLOTS = {1: {"scsi0"}, 2: {"sata0"}, 3: {"sata0"},
         4: {"efidisk0", "scsi0"}, 5: {"scsi0", "scsi1", "scsi2"},
         6: {"scsi0"}, 7: {"scsi0"}}
TARGETS = (1, 5, 6, 7)
SERVERS = (5, 7)
log = logging.getLogger("demo2-finalize")
state_lock = threading.RLock()
start_lock = threading.Lock()
stop_event = threading.Event()
reservations = {}
state = {}
inventory = {}
state_path = WORK / "state.json"

GUEST = r'''set -eu
export LC_ALL=C
die() { echo "ERROR: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "root is required"
[ -f /etc/altlinux-release ] || die "this guest is not recognized as ALT Linux"
for cmd in apt-get apt-config awk grep find mkdir cp mktemp chmod mv; do
    command -v "$cmd" >/dev/null 2>&1 || die "missing command: $cmd"
done
[ -d /etc/apt/apt.conf.d ] || die "apt.conf.d is missing"
mp=/mnt/additional
backup=/var/lib/spetsnaz-finalize
entry='LABEL=Additional /mnt/additional iso9660 ro,nofail,x-systemd.device-timeout=10s 0 0'
tmp=
trap '[ -z "$tmp" ] || rm -f -- "$tmp"' 0

if [ "$WITH_ISO" = 1 ]; then
    for cmd in blkid findmnt mount readlink ls; do
        command -v "$cmd" >/dev/null 2>&1 || die "missing command: $cmd"
    done
    dev=$(blkid -L Additional) || die "ISO label Additional was not found"
    [ "$(blkid -s TYPE -o value "$dev")" = iso9660 ] || die "Additional is not ISO9660"
    if awk 'NF >= 2 && $1 !~ /^#/ &&
        (($2 == "/mnt/additional" && $1 != "LABEL=Additional") ||
         ($1 == "LABEL=Additional" && $2 != "/mnt/additional")) {bad=1}
        END {exit !bad}' /etc/fstab; then
        die "conflicting entry in fstab"
    fi
    if mounted=$(findmnt -rn -M "$mp" -o SOURCE); then
        [ "$(readlink -f "$mounted")" = "$(readlink -f "$dev")" ] ||
            die "another device is mounted at /mnt/additional"
    elif [ "$MODE" = apply ]; then
        if [ -e "$mp" ]; then
            [ -d "$mp" ] && [ ! -L "$mp" ] || die "invalid mount directory"
            [ -z "$(ls -A "$mp")" ] || die "mount directory is not empty"
        fi
    else
        die "Additional did not mount automatically after the power cycle"
    fi
fi

if [ "$MODE" = apply ]; then
    mkdir -p "$backup"
    chmod 700 "$backup"
    if [ "$WITH_ISO" = 1 ]; then
        [ -e "$backup/fstab.before" ] || cp -p /etc/fstab "$backup/fstab.before"
        mkdir -p "$mp"
        tmp=$(mktemp /etc/fstab.spetsnaz.XXXXXX)
        awk -v entry="$entry" 'NF < 2 || $1 ~ /^#/ || $2 != "/mnt/additional" {print}
            END {print entry}' /etc/fstab > "$tmp"
        chmod 644 "$tmp"
        mv -f "$tmp" /etc/fstab
        tmp=
        if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload; fi
        findmnt -rn -M "$mp" >/dev/null || mount "$mp"
    fi
    if [ -f /etc/apt/apt.conf.d/99spetsnaz-proxy.conf ]; then
        [ -e "$backup/apt-proxy.before" ] ||
            cp -p /etc/apt/apt.conf.d/99spetsnaz-proxy.conf "$backup/apt-proxy.before"
    fi
    tmp=$(mktemp /etc/apt/apt.conf.d/.spetsnaz.XXXXXX)
    printf 'Acquire::http::Proxy "%s";\n' "$PROXY" > "$tmp"
    chmod 644 "$tmp"
    mv -f "$tmp" /etc/apt/apt.conf.d/99spetsnaz-proxy.conf
    tmp=
    # Same cleanup as apt_job(cache) in core.sh; the partial directory is retained.
    apt-get clean
    mkdir -p /var/lib/apt/lists/partial
    find /var/lib/apt/lists -type f -delete
    mkdir -p /var/lib/apt/lists/partial
fi

[ "$(cat /etc/apt/apt.conf.d/99spetsnaz-proxy.conf)" = \
    "$(printf 'Acquire::http::Proxy "%s";' "$PROXY")" ] || die "proxy file mismatch"
effective=$(apt-config shell SPZ_PROXY Acquire::http::Proxy)
expected_proxy=$(printf "SPZ_PROXY='%s'" "$PROXY")
[ "$effective" = "$expected_proxy" ] || [ "$effective" = "$expected_proxy;" ] ||
    die "the effective APT proxy differs from the requested proxy"
echo APT_PROXY=OK
[ -d /var/lib/apt/lists/partial ] || die "APT partial directory is missing"
if [ "$WITH_ISO" = 1 ]; then
    grep -Fqx "$entry" /etc/fstab || die "automount entry is missing"
    [ "$(findmnt -rn -M "$mp" -o FSTYPE)" = iso9660 ] || die "mount is missing"
    findmnt -rn -M "$mp" -o OPTIONS | tr ',' '\n' | grep -qx ro ||
        die "ISO is not mounted read-only"
    echo ISO_MOUNT=OK
else
    echo ISO_MOUNT=SKIP
fi

if [ "$MODE" = verify ]; then
    if command -v curl >/dev/null 2>&1; then
        if curl -fsS --noproxy '' --proxy "$PROXY" --connect-timeout 3 \
            --max-time 10 -o /dev/null "$RPM" 2>/dev/null; then
            echo CACHE_DOWNLOAD=OK
        else
            echo CACHE_DOWNLOAD=FAILED
        fi
    else
        echo CACHE_DOWNLOAD=NO_CURL
    fi
fi
echo GUEST_CHECK=OK
'''

def fail(message):
    raise RuntimeError(message)

def guard():
    if stop_event.is_set():
        fail("Обработка остановлена из-за ошибки в другом стенде")

def run(*args, input=None, timeout=60, check=True):
    result = subprocess.run(list(args), input=input, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    if check and result.returncode:
        fail(f"Команда завершилась с ошибкой: {shlex.join(args)}\n"
             f"{result.stderr[-2000:]}\n{result.stdout[-1000:]}")
    return result

def text(*args, **kwargs):
    return run(*args, **kwargs).stdout.strip()

def jget(path, *args):
    return json.loads(text("pvesh", "get", path, *args, "--output-format", "json"))

def vmids(stand):
    base = 20000 + (stand - 1) * 100
    return [base + n for n in range(1, 8)]

def config(vmid):
    raw = text("qm", "config", str(vmid))
    return dict(line.split(": ", 1) for line in raw.splitlines() if ": " in line)

def status(vmid):
    value = text("qm", "status", str(vmid))
    if value not in ("status: stopped", "status: running"):
        fail(f"VM {vmid}: неожиданное состояние {value}")
    return value.split()[-1]

def fingerprint(cfg):
    clean = {k: v for k, v in cfg.items() if k not in ("parent", "lock", "snapstate")}
    return hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()

def owned_disks(vmid, cfg):
    disks = {}
    for slot, value in cfg.items():
        if not re.fullmatch(r"(?:scsi|sata|virtio|ide|efidisk|tpmstate)\d+", slot):
            continue
        if "media=cdrom" in value or "cloudinit" in value.split(",")[0]:
            continue
        volume = value.split(",")[0]
        if not re.fullmatch(rf"ssd:{vmid}/vm-{vmid}-disk-\d+\.qcow2", volume):
            fail(f"VM {vmid}: требуется свой диск QCOW2 на ssd: {slot}: {volume}")
        disks[slot] = volume
    if set(disks) != SLOTS[vmid % 100]:
        fail(f"VM {vmid}: неожиданный набор дисков: {sorted(disks)}")
    return disks

def validate_cfg(vmid, cfg):
    if cfg.get("name") != ROLES[vmid % 100]:
        fail(f"VM {vmid}: имя не соответствует роли")
    if cfg.get("lock") or cfg.get("template") == "1":
        fail(f"VM {vmid}: блокировка или шаблон; автоматического unlock не будет")
    if any(re.fullmatch(r"unused\d+", key) for key in cfg):
        fail(f"VM {vmid}: найдены Unused, нужна отдельная проверка")
    disks = owned_disks(vmid, cfg)
    if vmid % 100 in TARGETS and cfg.get("agent", "0").split(",")[0] != "1":
        fail(f"VM {vmid}: гостевой агент не включён в настройках Proxmox")
    if vmid % 100 in SERVERS:
        cd = cfg.get("ide2", "none,media=cdrom")
        if cd.split(",")[0] not in (ISO, "none") or "media=cdrom" not in cd:
            fail(f"VM {vmid}: ide2 занят другим устройством")
    return disks

def snapshots(vmid):
    return {s["name"]: s for s in jget(f"/nodes/{NODE}/qemu/{vmid}/snapshot")
            if s["name"] != "current"}

def marker():
    return "SPZ-DEMO2-FINALIZE-v1:" + state["token"]

def validate_snapshots(vmid, stand):
    found = snapshots(vmid)
    if found and (set(found) != {"Day0"} or
                  found["Day0"].get("description") != marker() or
                  found["Day0"].get("snapstate") or
                  str(stand) not in state["prepared"]):
        fail(f"VM {vmid}: есть посторонние снимки; ничего не перезаписываю")
    return found

def save_state():
    with state_lock:
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, state_path)

def guest_exec(vmid, mode, iso):
    script = ("MODE=" + mode + "\nWITH_ISO=" + str(int(iso)) + "\nPROXY=" +
              shlex.quote(PROXY) + "\nRPM=" + shlex.quote(RPM) + "\n" + GUEST)
    response = json.loads(text("qm", "guest", "exec", str(vmid),
                               "--pass-stdin", "1", "--timeout", "90",
                               "--", "/bin/sh", "-s", input=script, timeout=110))
    response = response.get("result", response)
    deadline = time.monotonic() + 60
    while not response.get("exited"):
        pid = response.get("pid")
        if pid is None or time.monotonic() >= deadline:
            fail(f"VM {vmid}: гостевая команда не завершилась; повторно не запускаю")
        time.sleep(3)
        update = json.loads(text("qm", "guest", "exec-status", str(vmid), str(pid), timeout=20))
        response = update.get("result", update)
        if not response.get("exited"):
            response["pid"] = pid
    audit = WORK / f"guest-{vmid}-{mode}.json"
    audit.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
    if response.get("exitcode") != 0 or response.get("out-truncated") or response.get("err-truncated"):
        fail(f"VM {vmid}: гостевая проверка не прошла: "
             f"{response.get('err-data', '')[-1500:]} {response.get('out-data', '')[-1500:]}")
    report = dict(line.split("=", 1) for line in response.get("out-data", "").splitlines()
                  if re.match(r"^(APT_PROXY|ISO_MOUNT|CACHE_DOWNLOAD|GUEST_CHECK)=", line))
    if report.get("GUEST_CHECK") != "OK" or report.get("APT_PROXY") != "OK":
        fail(f"VM {vmid}: нет подтверждения применения патча")
    if iso and report.get("ISO_MOUNT") != "OK":
        fail(f"VM {vmid}: нет подтверждения монтирования ISO")
    return report

def wait_agents(ids):
    deadline = time.monotonic() + 300
    pending = set(ids)
    while pending:
        guard()
        for vmid in sorted(pending.copy()):
            if status(vmid) != "running":
                fail(f"VM {vmid} выключилась во время ожидания агента")
            try:
                result = run("qm", "agent", str(vmid), "ping", timeout=12, check=False)
                if result.returncode == 0:
                    pending.remove(vmid)
            except subprocess.TimeoutExpired:
                pass
        if not pending:
            return
        if time.monotonic() >= deadline:
            fail(f"Гостевые агенты не ответили за 300 с: {sorted(pending)}")
        log.info("Ожидаю гостевые агенты: %s", sorted(pending))
        time.sleep(10)

def available_memory():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    fail("Не удалось определить MemAvailable")

def start_group(stand, ids):
    with start_lock:
        guard()
        wanted = sum(int(config(v).get("memory", "512")) * 1024**2
                     for v in ids if status(v) == "stopped")
        if available_memory() < wanted + sum(v for k, v in reservations.items() if k != stand) + 4 * 1024**3:
            fail(f"Стенд {stand}: недостаточно свободной памяти")
        reservations[stand] = wanted
        for vmid in ids:
            guard()
            cfg = config(vmid)
            validate_cfg(vmid, cfg)
            if status(vmid) == "stopped":
                log.info("Стенд %s: включаю VM %s", stand, vmid)
                run("qm", "start", str(vmid), timeout=120)
                time.sleep(2)

def shutdown_group(ids):
    for vmid in ids:
        guard()
        if status(vmid) == "running":
            log.info("Мягко выключаю VM %s", vmid)
            run("qm", "shutdown", str(vmid), "--timeout", "120", timeout=150)
        if status(vmid) != "stopped":
            fail(f"VM {vmid} не выключилась; принудительного отключения не будет")

def prepare_stand(stand):
    guard()
    ids = vmids(stand)
    guests = [v for v in ids if v % 100 in TARGETS]
    for vmid in ids:
        validate_cfg(vmid, config(vmid))
        validate_snapshots(vmid, stand)
    key = str(stand)
    with state_lock:
        prepared = state["prepared"].get(key)
    if prepared:
        for vmid in ids:
            cfg = config(vmid)
            validate_cfg(vmid, cfg)
            if status(vmid) != "stopped" or fingerprint(cfg) != prepared["configs"][str(vmid)]:
                fail(f"Стенд {stand}: состояние изменилось после подготовки")
        log.info("Стенд %s: предыдущая подготовка подтверждена, продолжаю Day0", stand)
    else:
        log.info("Стенд %s: начинаю подготовку", stand)
        for vmid in guests:
            cfg = config(vmid)
            if vmid % 100 in SERVERS and cfg.get("ide2", "none").split(",")[0] != ISO:
                if status(vmid) == "running":
                    run("qm", "shutdown", str(vmid), "--timeout", "120", timeout=150)
                if status(vmid) != "stopped":
                    fail(f"VM {vmid}: нельзя безопасно подключить ISO")
                guard()
                run("qm", "set", str(vmid), "--ide2", ISO + ",media=cdrom")
        start_group(stand, guests)
        wait_agents(guests)
        for vmid in guests:
            guard()
            guest_exec(vmid, "apply", vmid % 100 in SERVERS)
            log.info("VM %s: патч APT и настройка ISO применены", vmid)
        shutdown_group(guests)
        log.info("Стенд %s: повторный запуск для проверки автомонтирования", stand)
        start_group(stand, guests)
        wait_agents(guests)
        reports = {}
        for vmid in guests:
            guard()
            report = guest_exec(vmid, "verify", vmid % 100 in SERVERS)
            reports[str(vmid)] = report
            log.info("VM %s: проверка после запуска OK; загрузка через кэшер: %s",
                     vmid, report.get("CACHE_DOWNLOAD", "UNKNOWN"))
        shutdown_group(guests)
        for vmid in ids:
            if status(vmid) != "stopped":
                fail(f"VM {vmid}: перед Day0 требуется выключенная ВМ")
        with state_lock:
            state["prepared"][key] = {
                "configs": {str(v): fingerprint(config(v)) for v in ids},
                "reports": reports}
            save_state()

    for vmid in ids:
        guard()
        cfg = config(vmid)
        validate_cfg(vmid, cfg)
        if status(vmid) != "stopped":
            fail(f"VM {vmid}: требуется выключенная ВМ перед Day0")
        if fingerprint(cfg) != state["prepared"][key]["configs"][str(vmid)]:
            fail(f"VM {vmid}: конфигурация изменилась перед Day0")
        for volume in owned_disks(vmid, cfg).values():
            path = text("pvesm", "path", volume)
            log.info("VM %s: проверяю целостность QCOW2 %s", vmid, volume)
            checked = json.loads(text("qemu-img", "check", "--output=json", path, timeout=180))
            if checked.get("corruptions", 0) or checked.get("check-errors", 0):
                fail(f"VM {vmid}: проверка целостности QCOW2 не прошла")
        snaps = snapshots(vmid)
        if snaps:
            if set(snaps) != {"Day0"} or snaps["Day0"].get("description") != marker():
                fail(f"VM {vmid}: обнаружен посторонний снимок; ничего не перезаписываю")
        else:
            log.info("VM %s: создаю Day0", vmid)
            run("qm", "snapshot", str(vmid), "Day0", "--description", marker(), timeout=600)
            created = snapshots(vmid).get("Day0", {})
            if created.get("description") != marker() or created.get("snapstate"):
                fail(f"VM {vmid}: нет подтверждения завершённого Day0")
        with state_lock:
            if vmid not in state["day0"]:
                state["day0"].append(vmid)
                save_state()
    with start_lock:
        reservations.pop(stand, None)
    log.info("Стенд %s ГОТОВ: APT 4/4, ISO 2/2, Day0 7/7; все ВМ выключены", stand)

def parse_conf(content):
    values = {}
    for line in content.splitlines():
        match = re.match(r"^\s*(WAN_BR|TARGET_STORAGE|APT_MODE|PROXY_IP|APT_CACHE_PORT)\s*=(.*)$", line)
        if match:
            parts = shlex.split(match[2], comments=True)
            if len(parts) != 1:
                fail(f"Неоднозначная настройка {match[1]} в spetsnaz.conf")
            values[match[1]] = parts[0]
    return values

def preflight():
    expected = {"WAN_BR": "vmbr0", "TARGET_STORAGE": "ssd", "APT_MODE": "proxy",
                "PROXY_IP": "10.0.55.12", "APT_CACHE_PORT": "3142"}
    conf = parse_conf((REPO / "spetsnaz.conf").read_text())
    if any(conf.get(k) != v for k, v in expected.items()):
        fail("spetsnaz.conf отличается от согласованных настроек vmbr0/ssd/proxy 10.0.55.12:3142")
    core = (REPO / "core.sh").read_text()
    required = ("apt_job()", "99spetsnaz-proxy", "Acquire::http::Proxy",
                "apt-get clean", "find /var/lib/apt/lists -type f -delete",
                "mkdir -p /var/lib/apt/lists/partial", "ISP|HQ-SRV|HQ-CLI|BR-SRV")
    if not all(item in core for item in required):
        fail("Логика локального core.sh отличается от проверенной версии")
    log.info("APT: логика локального core.sh, SHA256=%s",
             hashlib.sha256(core.encode()).hexdigest())
    storage = jget("/storage/ssd")
    if storage.get("type") != "dir" or storage.get("path") != "/mnt/ssd":
        fail("ssd не является ожидаемым dir-хранилищем /mnt/ssd")
    if not Path("/mnt/ssd").is_mount() or shutil.disk_usage("/mnt/ssd").free < 32 * 1024**3:
        fail("ssd не смонтирован или свободно менее 32 ГиБ")
    iso_path = Path(text("pvesm", "path", ISO))
    if not iso_path.is_file() or iso_path.stat().st_size == 0:
        fail("Не найден local:iso/Additional.iso")
    if text("systemctl", "is-active", "apt-cacher-ng") != "active":
        fail("Служба apt-cacher-ng не активна")
    with socket.create_connection(("10.0.55.12", 3142), timeout=5):
        pass

    old_file = Path("/root/pre-it-spetsnaz/old-vms.tsv")
    old = set()
    if old_file.is_file():
        for line in old_file.read_text().splitlines():
            fields = line.split("\t")
            if len(fields) > 1 and fields[1].isdigit():
                old.add(int(fields[1]))
    all_ids = {v for s in range(1, 16) for v in vmids(s)}
    if all_ids & old:
        fail("Новые VMID пересекаются с инвентарём старых ВМ")

    for stand in range(1, 16):
        guard()
        data = jget(f"/pools/DEMO_2_S{stand}")
        members = [m for m in data.get("members", []) if m.get("type") == "qemu"]
        if (sorted(int(m["vmid"]) for m in members) != vmids(stand) or
                any(m.get("node") != NODE for m in members)):
            fail(f"DEMO_2_S{stand}: неожиданный состав или узел")
        count = 0
        for vmid in vmids(stand):
            cfg = config(vmid)
            disks = validate_cfg(vmid, cfg)
            vmstatus = status(vmid)
            if vmid % 100 not in TARGETS and vmstatus != "stopped":
                fail(f"VM {vmid}: роутеры и BR-FW должны оставаться выключенными")
            validate_snapshots(vmid, stand)
            if str(stand) in state["prepared"]:
                saved = state["prepared"][str(stand)]["configs"][str(vmid)]
                if fingerprint(cfg) != saved or vmstatus != "stopped":
                    fail(f"VM {vmid}: ранее подготовленная конфигурация изменилась")
            inventory[vmid] = dict(cfg)
            backup = WORK / "configs" / f"{vmid}.conf"
            if not backup.exists():
                backup.write_text(text("qm", "config", str(vmid)) + "\n", encoding="utf-8")
            for volume in disks.values():
                path = Path(text("pvesm", "path", volume))
                if path.resolve().parent != Path(f"/mnt/ssd/images/{vmid}") or not path.is_file():
                    fail(f"VM {vmid}: неожиданный путь диска {volume}")
                args = ["qemu-img", "info", "--output=json"]
                if vmstatus == "running":
                    args.append("--force-share")
                info = json.loads(text(*args, str(path)))
                if info.get("format") != "qcow2" or info.get("backing-filename"):
                    fail(f"VM {vmid}: диск не самостоятельный QCOW2: {volume}")
                count += 1
        log.info("Проверка стенда %s/15: 7 ВМ, %s дисков QCOW2 — OK", stand, count)
    log.info("ПРЕДВАРИТЕЛЬНАЯ ПРОВЕРКА ПРОЙДЕНА: 105 ВМ, 150 дисков QCOW2")

def summary():
    for stand in range(1, 16):
        for vmid in vmids(stand):
            cfg = config(vmid)
            validate_cfg(vmid, cfg)
            if status(vmid) != "stopped" or snapshots(vmid).get("Day0", {}).get("description") != marker():
                fail(f"VM {vmid}: итоговая проверка Day0/состояния не прошла")
            if vmid % 100 in SERVERS and cfg.get("ide2", "").split(",")[0] != ISO:
                fail(f"VM {vmid}: итоговая проверка ISO не прошла")
    reports = [r for prepared in state["prepared"].values()
               for r in prepared["reports"].values()]
    ok = sum(r.get("CACHE_DOWNLOAD") == "OK" for r in reports)
    log.info("ГОТОВО: стендов 15/15; QCOW2 150/150; APT 60/60; ISO 30/30; Day0 105/105.")
    log.info("Все 105 новых ВМ выключены. Unused нет.")
    log.info("Загрузка тестового RPM через кэшер из гостей: %s/60.", ok)
    if ok != 60:
        failed = {vmid: r.get("CACHE_DOWNLOAD", "UNKNOWN")
                  for prepared in state["prepared"].values()
                  for vmid, r in prepared["reports"].items() if r.get("CACHE_DOWNLOAD") != "OK"}
        log.warning("Настройки APT сохранены; загрузка RPM не подтверждена у: %s", failed)
        log.warning("Учебная сеть не изменялась. Проверьте маршрутизацию/доступ к репозиторию отдельно.")
    usage = shutil.disk_usage("/mnt/ssd")
    log.info("Свободно на ssd: %.1f ГиБ. Журнал: %s", usage.free / 1024**3, WORK / "run.log")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        fail("Запускайте на Proxmox от root")
    import fcntl
    os.umask(0o077)
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "configs").mkdir(exist_ok=True)
    lock_file = (WORK / "run.lock").open("a")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fail("Другой экземпляр этого скрипта уже работает")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(WORK / "run.log", encoding="utf-8")])
    if state_path.exists():
        state.update(json.loads(state_path.read_text()))
        if state.get("version") != 1 or not state.get("token"):
            fail("Неподдерживаемый файл состояния")
    else:
        state.update(version=1, token=uuid.uuid4().hex, prepared={}, day0=[])
        save_state()
    preflight()
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(prepare_stand, s): s for s in range(1, 16)}
        try:
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except concurrent.futures.CancelledError:
                    continue
                except Exception as exc:
                    stop_event.set()
                    errors.append((futures[future], str(exc)))
                    log.error("СТОП, стенд %s: %s", futures[future], exc)
                    for pending in futures:
                        pending.cancel()
        except KeyboardInterrupt:
            stop_event.set()
            for pending in futures:
                pending.cancel()
            raise
    if errors:
        fail(f"Подготовка не завершена. Подробности: {WORK / 'run.log'}. "
             "Некоторые новые ВМ могут оставаться включёнными. Снимки не перезаписывались.")
    summary()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        stop_event.set()
        log.error("Прервано пользователем. Повторный запуск продолжит обработку; проверьте run.log.")
        sys.exit(130)
    except Exception as exc:
        log.error("СТОП: %s", exc)
        sys.exit(1)
