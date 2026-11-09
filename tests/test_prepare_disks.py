"""Disk conversion and cleanup simulations. No Proxmox or qemu-img required."""
import importlib.util
import json
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare", ROOT / "scripts/prepare_disks.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Sim:
    def __init__(self, directory, all_vms=False, qcow=False):
        self.root = Path(directory)
        self.cfg, self.files, self.calls = {}, {}, []
        self.power = "stopped"
        self.snaps = {}
        self.error = None
        ids = [v for s in range(1, 16) for v in m.f.vmids(s)] if all_vms else [20005]
        for v in ids:
            c = {"name": m.f.ROLES[v % 100], "agent": "1", "memory": "1024"}
            for n, slot in enumerate(sorted(m.f.SLOTS[v % 100])):
                volume = f"ssd:{v}/vm-{v}-disk-{n}." + ("qcow2" if qcow else "raw")
                c[slot] = volume + ",size=1G"
                self.add(volume)
            c["ide0"] = f"ssd:{v}/vm-{v}-cloudinit.raw,media=cdrom"
            self.cfg[v] = c
        m.WORK = self.root
        m.JOURNAL = self.root / "state.json"
        m.plan = {"version": 1, "vms": {str(v): {
            "metadata": m.metadata(v, c),
            "disks": {slot: {"source": volume, "size": 1024**3}
                      for slot, volume in m.working(v, c).items()}}
            for v, c in self.cfg.items()}}
        m.f.config = lambda v: dict(self.cfg[v])
        m.f.status = lambda v: self.power
        m.f.snapshots = lambda v: self.snaps
        m.f.jget = self.jget
        m.f.run = self.run
        m.f.text = self.text
        m.local_path = self.path
        m.shutil.disk_usage = lambda p: types.SimpleNamespace(free=1024**4)

    def path(self, v, volume):
        m.disk_volume(v, volume)
        return self.root / volume.replace(":", "_").replace("/", "_")

    def add(self, volume):
        self.files[volume] = True
        self.path(int(volume.split(":")[1].split("/")[0]), volume).touch()

    def jget(self, path, *args):
        stand = int(path.split("_S")[1])
        return {"members": [{"vmid": v, "type": "qemu", "node": "pve"} for v in m.f.vmids(stand)]}

    def text(self, *args, **kwargs):
        if args[:2] == ("qemu-img", "info"):
            return json.dumps({"format": "raw" if args[-1].endswith(".raw") else "qcow2", "virtual-size": 1024**3})
        if args[:2] == ("qemu-img", "check"):
            self.calls.append(args)
            if self.error == "check":
                raise RuntimeError("bad QCOW2")
            return '{}'
        raise AssertionError(args)

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if args[:3] == ("qm", "disk", "move"):
            assert args[-4:] == ("--format", "qcow2", "--delete", "0")
            vid, slot = int(args[3]), args[4]
            assert vid in self.cfg and slot in m.f.SLOTS[vid % 100]
            source = self.cfg[vid][slot].split(",")[0]
            target = source[:-4] + ".qcow2"
            self.add(target)
            self.cfg[vid][slot] = target + ",size=1G"
            self.cfg[vid]["unused0"] = source
            if self.error == "move-result":
                raise RuntimeError("connection lost after move")
            return
        if args[:2] == ("qemu-img", "compare"):
            assert args[2:6] == ("-f", "raw", "-F", "qcow2")
            assert Path(args[6]).is_file() and Path(args[7]).is_file()
            if self.error == "compare":
                raise RuntimeError("different contents")
            return
        if args[:3] == ("qm", "disk", "unlink"):
            if self.error == "unlink":
                raise RuntimeError("failed before unlink")
            vid, slot = int(args[3]), args[-1]
            assert args[4] == "--idlist" and slot.startswith("unused")
            source = self.cfg[vid].pop(slot)
            entry = next(e for e in m.plan["vms"][str(vid)]["disks"].values() if e["source"] == source)
            assert entry.get("verified"), "deletion without verification"
            assert any(c[:2] == ("qemu-img", "compare") for c in self.calls)
            self.path(vid, source).unlink()
            if self.error == "unlink-result":
                raise RuntimeError("connection lost after unlink")
            return
        raise AssertionError(args)


def raises(fn):
    try:
        fn()
    except RuntimeError:
        return
    raise AssertionError("expected failure")


def first():
    slot = next(iter(m.plan["vms"]["20005"]["disks"]))
    return slot, m.plan["vms"]["20005"]["disks"][slot]


with tempfile.TemporaryDirectory() as d:
    sim = Sim(d)
    slot, entry = first()
    source = sim.path(20005, entry["source"])
    m.prepare_one(20005, slot, entry)
    assert not source.exists() and entry["done"] and entry["verified"]
    before = len(sim.calls)
    m.prepare_one(20005, slot, entry)
    assert not any(a[:3] in (("qm", "disk", "move"), ("qm", "disk", "unlink")) for a in sim.calls[before:])
print("PASS: move retains RAW, compare/check precede cleanup, rerun does not move/delete again")

for error in ("check", "compare", "move-result", "unlink", "unlink-result"):
    with tempfile.TemporaryDirectory() as d:
        sim = Sim(d)
        slot, entry = first()
        sim.error = error
        raises(lambda: m.prepare_one(20005, slot, entry))
        if error in ("check", "compare", "move-result", "unlink"):
            assert sim.path(20005, entry["source"]).is_file()
        if error in ("check", "compare", "move-result"):
            assert not any(a[:3] == ("qm", "disk", "unlink") for a in sim.calls)
        sim.error = None
        m.prepare_one(20005, slot, entry)
        assert entry["done"] and not sim.path(20005, entry["source"]).exists()
print("PASS: five failure/resume paths retain source until verified, including uncertain move/unlink results")

for condition in ("running", "snapshot", "unused", "foreign"):
    with tempfile.TemporaryDirectory() as d:
        sim = Sim(d)
        slot, entry = first()
        if condition == "running": sim.power = "running"
        if condition == "snapshot": sim.snaps = {"Day0": {"name": "Day0"}}
        if condition == "unused": sim.cfg[20005]["unused1"] = "ssd:20005/untracked.raw"
        if condition == "foreign": sim.cfg[20005][slot] = "ssd:10101/vm-10101-disk-0.raw"
        raises(lambda: m.prepare_one(20005, slot, entry))
        assert not sim.calls
print("PASS: running VMs, snapshots, unknown Unused and foreign disks fail before writes")

with tempfile.TemporaryDirectory() as d:
    sim = Sim(d, qcow=True)
    slot, entry = first()
    m.prepare_one(20005, slot, entry)
    assert entry["done"] and not any(a[0] == "qm" for a in sim.calls)
print("PASS: existing QCOW2 checked without conversion or cleanup")

with tempfile.TemporaryDirectory() as d:
    sim = Sim(d, all_vms=True)
    # Exercise initial discovery rather than a hand-prepared journal.
    m.plan = {}
    assert m.inspect() == 150
    m.save()
    for key, record in m.plan["vms"].items():
        for slot, entry in record["disks"].items():
            m.prepare_one(int(key), slot, entry)
    assert sum(c[:3] == ("qm", "disk", "move") for c in sim.calls) == 150
    assert sum(c[:3] == ("qm", "disk", "unlink") for c in sim.calls) == 150
    assert m.inspect() == 0
print("PASS: exact 15-pool discovery, 105 VMs / 150 disks, persisted journal and completed resume")
