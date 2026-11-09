#!/usr/bin/env python3
"""Read-only checks BEFORE a fresh deployment of our 15-stand profile."""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("finalizer", ROOT / "finalize_demo2.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    require(os.name == "posix" and os.geteuid() == 0, "Запускайте от root на Proxmox")
    require(socket.gethostname().split(".")[0] == "pve", "Профиль рассчитан на узел pve")
    require(ROOT == Path("/root/proxmox-auto-deploy"), "Установите проект в /root/proxmox-auto-deploy")
    for cmd in ("pvesh", "pvesm", "qm", "qmrestore", "qemu-img", "jq", "zstdcat",
                "ovs-vsctl", "systemctl", "blkid"):
        require(shutil.which(cmd), "Не найдена команда: " + cmd)
    expected = {"WAN_BR": "vmbr0", "TARGET_STORAGE": "ssd", "APT_MODE": "proxy",
                "PROXY_IP": "10.0.55.12", "APT_CACHE_PORT": "3142"}
    cfg = f.parse_conf((ROOT / "spetsnaz.conf").read_text())
    require(all(cfg.get(k) == v for k, v in expected.items()), "spetsnaz.conf отличается от нашего профиля")
    # Read config as data, never source a configuration from Python.
    import shlex
    other = {}
    for line in (ROOT / "spetsnaz.conf").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            parts = shlex.split(v, comments=True)
            if len(parts) == 1:
                other[k.strip()] = parts[0]
    require(other.get("IMG_FROM_DEMO_2") == "DEMO_1", "Нужно IMG_FROM_DEMO_2=DEMO_1")
    require(other.get("IMG_DIR") == "/mnt/pve/VM_Disk/spetsnaz-images", "Неожиданная папка образов")
    img = Path(other["IMG_DIR"])
    require(not (img / "DEMO_2").exists(), "DEMO_2 должен использовать DEMO_1: найдена своя папка DEMO_2")
    for role in f.ROLES.values():
        archive = img / "DEMO_1" / (role + ".vma.zst")
        require(archive.is_file() and archive.stat().st_size > 0, "Нет образа: " + str(archive))
    all_vms = f.jget("/cluster/resources", "--type", "vm")
    used = {int(v["vmid"]) for v in all_vms}
    conflict = sorted(used & set(range(20000, 21500)))
    require(not conflict, "Диапазон 20000-21499 занят: " + str(conflict))
    pools = {p["poolid"] for p in f.jget("/pools")}
    require(not pools & {f"DEMO_2_S{s}" for s in range(1, 16)}, "Пулы DEMO_2 уже существуют: это не новая установка")
    bridges = {f"vmbr{20000 + (s-1)*100 + n}" for s in range(1, 16) for n in range(1, 11)}
    existing = {n["iface"] for n in f.jget("/nodes/pve/network")}
    existing |= {p.name for p in Path("/sys/class/net").iterdir()}
    require(not bridges & existing, "Есть конфликтующие мосты: " + str(sorted(bridges & existing)))
    require("vmbr0" in existing, "Не найден WAN vmbr0")
    for storage, path in (("ssd", "/mnt/ssd"), ("VM_Disk", "/mnt/pve/VM_Disk")):
        info = f.jget("/storage/" + storage)
        require(info.get("type") == "dir" and info.get("path") == path, "Неожиданное хранилище: " + storage)
        require(Path(path).is_mount(), "Хранилище не смонтировано: " + path)
    require(shutil.disk_usage("/mnt/ssd").free >= 600 * 1024**3, "Для этого профиля нужно минимум 600 ГиБ свободного места на ssd")
    for volume in f.jget("/nodes/pve/storage/ssd/content"):
        vmid = volume.get("vmid")
        require(vmid is None or not 20000 <= int(vmid) < 21500, "Есть остаточный том: " + volume.get("volid", "?"))
    iso = Path(f.text("pvesm", "path", f.ISO))
    require(iso.is_file(), "Не найден local:iso/Additional.iso")
    require(f.text("blkid", "-p", "-s", "LABEL", "-o", "value", str(iso)) == "Additional", "ISO должен иметь метку Additional")
    for service in ("openvswitch-switch", "apt-cacher-ng"):
        require(f.text("systemctl", "is-active", service) == "active", "Служба неактивна: " + service)
    with socket.create_connection(("10.0.55.12", 3142), timeout=5):
        pass
    old = Path("/root/pre-it-spetsnaz/old-vms.tsv")
    count = 0
    if old.is_file():
        for line in old.read_text().splitlines():
            node, vid, *_ = line.split("\t")
            require(int(vid) in used, "Старая ВМ отсутствует: " + vid)
            config = f.jget(f"/nodes/{node}/qemu/{vid}/config")
            require(int(config.get("protection", 0)) == 1, "Нет защиты старой ВМ: " + vid)
            count += 1
    print(f"Старые ВМ в реестре: {count}. Проверка ничего не изменяла.")
    print("ГОТОВО: диапазон VMID и мосты свободны; образы, ISO, хранилища и кэшер проверены.")
    print("Можно развернуть DEMO_2, номера 1-15 строго по порядку; ВМ пока не включать.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("СТОП: " + str(exc), file=sys.stderr)
        sys.exit(1)
