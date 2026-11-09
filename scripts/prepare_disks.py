#!/usr/bin/env python3
"""Prepare only our 105 stopped DEMO_2 VMs: RAW -> verified QCOW2.
Default is a read-only plan. --apply moves a disk via Proxmox, compares its
contents, then deletes only the recorded source in an unused slot via Proxmox.
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("finalizer", ROOT / "finalize_demo2.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
WORK = Path("/root/pre-it-spetsnaz/demo2-disks")
JOURNAL = WORK / "state.json"
plan = {}


def need(ok, message):
    if not ok:
        raise RuntimeError(message)


def disk_volume(vid, value):
    volume = value.split(",")[0]
    need(re.fullmatch(rf"ssd:{vid}/vm-{vid}-disk-[0-9]+\.(raw|qcow2)", volume),
         f"VM {vid}: unexpected or foreign disk: {volume}")
    return volume


def working(vid, cfg):
    result = {}
    for key, value in cfg.items():
        if not re.fullmatch(r"(?:scsi|sata|virtio|ide|efidisk|tpmstate)[0-9]+", key):
            continue
        if "media=cdrom" in value or "cloudinit" in value.split(",")[0]:
            continue
        result[key] = disk_volume(vid, value)
    need(set(result) == f.SLOTS[vid % 100], f"VM {vid}: unexpected disk slots")
    need(len(set(result.values())) == len(result), f"VM {vid}: duplicate disk volume")
    return result


def metadata(vid, cfg):
    return {k: v for k, v in cfg.items()
            if k not in f.SLOTS[vid % 100] and not re.fullmatch(r"unused[0-9]+", k)}


def local_path(vid, volume):
    disk_volume(vid, volume)
    path = Path(f.text("pvesm", "path", volume))
    need(not path.is_symlink() and path.resolve().parent == Path(f"/mnt/ssd/images/{vid}"),
         f"VM {vid}: unexpected disk path {path}")
    return path


def info(vid, volume):
    path = local_path(vid, volume)
    need(path.is_file(), f"VM {vid}: missing disk {volume}")
    data = json.loads(f.text("qemu-img", "info", "--output=json", str(path)))
    need(not data.get("backing-filename"), f"VM {vid}: backing file is not supported")
    expected = "raw" if volume.endswith(".raw") else "qcow2"
    need(data.get("format") == expected, f"VM {vid}: disk extension/format mismatch")
    need(data.get("virtual-size", 0) > 0, f"VM {vid}: invalid virtual size")
    return data


def stopped(vid):
    cfg = f.config(vid)
    need(cfg.get("name") == f.ROLES[vid % 100], f"VM {vid}: unexpected name")
    need(not cfg.get("lock") and cfg.get("template") != "1", f"VM {vid}: lock/template")
    need(f.status(vid) == "stopped", f"VM {vid}: must be stopped")
    need(not f.snapshots(vid), f"VM {vid}: snapshots already exist; preparation refused")
    working(vid, cfg)
    return cfg


def save():
    temporary = JOURNAL.with_suffix(".tmp")
    temporary.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    os.replace(temporary, JOURNAL)


def inspect():
    global plan
    for stand in range(1, 16):
        members = f.jget(f"/pools/DEMO_2_S{stand}").get("members", [])
        need(len(members) == 7 and sorted(int(v.get("vmid", -1)) for v in members) == f.vmids(stand)
             and all(v.get("node") == "pve" and v.get("type") == "qemu" for v in members),
             f"DEMO_2_S{stand}: unexpected members")
    ids = {str(v) for s in range(1, 16) for v in f.vmids(s)}
    if JOURNAL.exists():
        plan = json.loads(JOURNAL.read_text())
        need(plan.get("version") == 1 and set(plan.get("vms", {})) == ids, "Unexpected conversion journal")
    else:
        vms = {}
        for key in sorted(ids, key=int):
            vid = int(key)
            cfg = stopped(vid)
            need(not any(re.fullmatch(r"unused[0-9]+", k) for k in cfg), f"VM {vid}: unknown Unused")
            vms[key] = {"metadata": metadata(vid, cfg), "disks": {
                slot: {"source": volume, "size": info(vid, volume)["virtual-size"]}
                for slot, volume in working(vid, cfg).items()}}
        plan = {"version": 1, "vms": vms}
    for key, record in plan["vms"].items():
        vid = int(key)
        need(set(record["disks"]) == f.SLOTS[vid % 100], "Unexpected journal disk slots")
        for entry in record["disks"].values():
            disk_volume(vid, entry["source"])
        checked_config(vid)
    return sum(e["source"].endswith(".raw") and not e.get("done")
               for vm in plan["vms"].values() for e in vm["disks"].values())


def checked_config(vid):
    cfg = stopped(vid)
    record = plan["vms"][str(vid)]
    need(metadata(vid, cfg) == record["metadata"], f"VM {vid}: configuration changed")
    active = working(vid, cfg)
    allowed = {e["source"] for e in record["disks"].values() if e["source"].endswith(".raw")}
    for slot, value in cfg.items():
        if re.fullmatch(r"unused[0-9]+", slot):
            need(value in allowed and value not in active.values(), f"VM {vid}: unrecorded Unused {slot}")
    for slot, entry in record["disks"].items():
        current = active[slot]
        if current != entry["source"]:
            need(entry["source"].endswith(".raw") and current.endswith(".qcow2"), f"VM {vid}: disk changed")
            need(not entry.get("target") or entry["target"] == current, f"VM {vid}: target changed")
    return cfg


def check_qcow(vid, volume, size):
    need(volume.endswith(".qcow2"), "QCOW2 required")
    need(info(vid, volume)["virtual-size"] == size, f"VM {vid}: virtual size changed")
    data = json.loads(f.text("qemu-img", "check", "--output=json", str(local_path(vid, volume)), timeout=600))
    need(not data.get("corruptions") and not data.get("check-errors"), f"VM {vid}: damaged QCOW2")


def prepare_one(vid, slot, entry):
    cfg = checked_config(vid)
    source = entry["source"]
    current = working(vid, cfg)[slot]
    if source.endswith(".qcow2"):
        need(current == source, "Existing QCOW2 changed")
        check_qcow(vid, current, entry["size"])
        entry["done"] = True
        save()
        return
    old_path = local_path(vid, source)
    if current == source:
        need(not entry.get("verified"), "Verified target was replaced by RAW")
        need(info(vid, source)["virtual-size"] == entry["size"], "Source size changed")
        need(shutil.disk_usage("/mnt/ssd").free >= entry["size"] + 32 * 1024**3,
             "Insufficient temporary free space for conversion")
        print(f"VM {vid} {slot}: RAW -> QCOW2; source retained for verification", flush=True)
        f.run("qm", "disk", "move", str(vid), slot, "ssd", "--format", "qcow2", "--delete", "0", timeout=7200)
        cfg = checked_config(vid)
        current = working(vid, cfg)[slot]
    need(current != source and current.endswith(".qcow2"), "No QCOW2 target after move")
    unused = [k for k, v in cfg.items() if re.fullmatch(r"unused[0-9]+", k) and v == source]
    check_qcow(vid, current, entry["size"])
    if not unused and not old_path.exists():
        need(entry.get("verified") and entry.get("target") == current, "Source missing without recorded verification")
        entry["done"] = True
        save()
        return
    need(len(unused) == 1 and old_path.is_file(), "Expected exactly one recorded RAW Unused")
    need(info(vid, source)["virtual-size"] == entry["size"], "Source size changed")
    f.run("qemu-img", "compare", "-f", "raw", "-F", "qcow2", str(old_path),
          str(local_path(vid, current)), timeout=7200)
    entry.update(target=current, verified=True)
    save()
    # Recheck immediately before deleting the exact recorded, detached source.
    now = checked_config(vid)
    need(working(vid, now)[slot] == current and now.get(unused[0]) == source,
         "Disk configuration changed before cleanup")
    need(unused[0].startswith("unused"), "Only detached sources may be removed")
    f.run("qm", "disk", "unlink", str(vid), "--idlist", unused[0], timeout=300)
    need(not old_path.exists() and source not in f.config(vid).values(), "RAW cleanup not confirmed; inspect before retrying")
    entry["done"] = True
    save()
    print(f"VM {vid} {slot}: compared and checked; recorded RAW removed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Convert and remove only verified source RAW disks")
    args = parser.parse_args()
    need(os.name == "posix" and os.geteuid() == 0, "Run as root on Proxmox")
    storage = f.jget("/storage/ssd")
    need(storage.get("type") == "dir" and storage.get("path") == "/mnt/ssd" and Path("/mnt/ssd").is_mount(),
         "Expected mounted dir storage ssd at /mnt/ssd")
    if not args.apply:
        count = inspect()
        print(f"PLAN: 105 VMs; RAW disks still to prepare: {count}; no changes made. Use --apply to proceed.")
        return
    import fcntl
    os.umask(0o077)
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inspect()
        save()  # Record every source BEFORE the first move.
        for key in sorted(plan["vms"], key=int):
            vid = int(key)
            for slot, entry in plan["vms"][key]["disks"].items():
                prepare_one(vid, slot, entry)
        print("ГОТОВО: 150 рабочих QCOW2 проверены; записанные исходные RAW удалены. Day0 ещё не создавался.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("STOP: interrupted; retain journal and inspect Tasks before retrying", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        sys.exit(1)
