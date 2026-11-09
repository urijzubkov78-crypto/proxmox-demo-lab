import concurrent.futures as cf
import importlib.util
import os
import shutil
import json
import subprocess
import threading
import types
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("finish", ROOT / "finalize_demo2.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
TESTROOT = ROOT / "tmp" / ("demo2-tests-" + uuid.uuid4().hex[:8])
assert TESTROOT.resolve().is_relative_to((ROOT / "tmp").resolve())
TESTROOT.mkdir(parents=True)
real_sleep = __import__("time").sleep

class Simulation:
    def __init__(self, name):
        self.work = TESTROOT / name
        self.work.mkdir()
        self.cfg, self.power, self.snaps, self.boots = {}, {}, {}, {}
        self.applied, self.verified, self.calls = set(), set(), []
        self.clock = 0
        self.bad_guest = self.bad_agent = self.bad_disk = self.async_vm = None
        self.async_jobs = {}
        self.lock = threading.RLock()
        for stand in range(1, 16):
            for v in m.vmids(stand):
                n = v % 100
                c = {"name":m.ROLES[n],"memory":"1024","scsihw":"virtio-scsi-single"}
                for i, slot in enumerate(sorted(m.SLOTS[n])):
                    c[slot] = f"ssd:{v}/vm-{v}-disk-{i}.qcow2,size=1G"
                if n in m.TARGETS:
                    c["agent"] = "1"
                    c["ide0"] = f"ssd:{v}/vm-{v}-cloudinit.raw,media=cdrom,size=4M"
                if n in m.SERVERS:
                    c["ide2"] = m.ISO + ",media=cdrom"
                self.cfg[v], self.power[v], self.snaps[v], self.boots[v] = c,"stopped",{},0
        m.WORK = self.work
        m.state_path = self.work / "state.json"
        m.state.clear()
        m.state.update(version=1,token="simulation-token",prepared={},day0=[])
        m.reservations.clear()
        m.stop_event.clear()
        m.run = self.run
        m.available_memory = lambda: 1024**4
        m.time = types.SimpleNamespace(sleep=self.sleep,monotonic=lambda:self.clock)

    def sleep(self, n):
        with self.lock:
            self.clock += n
        real_sleep(0.0001)

    def run(self, *a, input=None, timeout=60, check=True):
        with self.lock:
            self.calls.append(a)
            def done(data="", code=0):
                if code and check:
                    raise RuntimeError("simulated command failure")
                return subprocess.CompletedProcess(a,code,data,"simulated error" if code else "")
            if a[0] == "pvesm" and a[1] == "path":
                return done("mock-image-" + a[2].replace("/","_"))
            if a[:2] == ("qemu-img","check"):
                if self.bad_disk is not None and f"{self.bad_disk}_" in a[-1]:
                    return done("{}",2)
                return done(json.dumps({"corruptions":0,"check-errors":0}))
            if a[:2] == ("pvesh","get"):
                v = int(a[2].split("/")[4])
                assert v in self.cfg
                return done(json.dumps([{"name":"current"}] + list(self.snaps[v].values())))
            assert a[0] == "qm", a
            op = a[1]
            v = int(a[3] if op == "guest" else a[2])
            assert v in self.cfg, ("OUT OF SCOPE",v)
            if op == "config":
                return done("\n".join(f"{k}: {val}" for k,val in self.cfg[v].items()))
            if op == "status":
                return done("status: " + self.power[v])
            if op == "start":
                assert v % 100 in m.TARGETS
                self.power[v] = "running"
                self.boots[v] += 1
                return done()
            if op == "shutdown":
                assert "--timeout" in a
                self.power[v] = "stopped"
                return done()
            if op == "set":
                assert a[3] == "--ide2" and v % 100 in m.SERVERS
                self.cfg[v]["ide2"] = a[4]
                return done()
            if op == "agent":
                return done("",1 if v == self.bad_agent else 0)
            if op == "snapshot":
                assert self.power[v] == "stopped"
                s = (v - 20000) // 100 + 1
                assert all(x in self.verified for x in m.vmids(s) if x % 100 in m.TARGETS)
                assert not self.snaps[v], "must not overwrite snapshots"
                self.snaps[v]["Day0"] = {"name":"Day0","description":a[-1]}
                self.cfg[v]["parent"] = "Day0"
                return done()
            if op == "guest" and a[2] == "exec-status":
                return done(json.dumps(self.async_jobs[int(a[4])]))
            if op == "guest" and a[2] == "exec":
                assert "--pass-stdin" in a and a[-2:] == ("/bin/sh","-s")
                assert self.power[v] == "running"
                mode = input.splitlines()[0].split("=")[1]
                iso = "WITH_ISO=1" in input.splitlines()
                if v == self.bad_guest:
                    return done(json.dumps({"exited":1,"exitcode":2,"err-data":"simulated guest failure"}))
                if mode == "apply":
                    self.applied.add(v)
                else:
                    assert v in self.applied and self.boots[v] >= 2, "must verify after power cycle"
                    self.verified.add(v)
                out = "APT_PROXY=OK\nISO_MOUNT=" + ("OK" if iso else "SKIP") + "\nGUEST_CHECK=OK\n"
                if mode == "verify":
                    out += "CACHE_DOWNLOAD=OK\n"
                response = {"exited":1,"exitcode":0,"out-data":out}
                if v == self.async_vm and mode == "apply":
                    self.async_jobs[42] = response
                    self.async_vm = None
                    return done(json.dumps({"pid":42}))
                return done(json.dumps(response))
            raise AssertionError(a)

def raises(fn, text=None):
    try:
        fn()
    except RuntimeError as e:
        if text:
            assert text in str(e), (text,str(e))
    else:
        raise AssertionError("expected RuntimeError")

checks=0
def passed(label):
    global checks
    checks += 1
    print("PASS:",label)

sim=Simulation("selection")
allids=[v for s in range(1,16) for v in m.vmids(s)]
assert len(set(allids))==105 and min(allids)==20001 and max(allids)==21407
assert sum(len(m.owned_disks(v,sim.cfg[v])) for v in allids)==150
assert len([v for v in allids if v%100 in m.TARGETS])==60
assert len([v for v in allids if v%100 in m.SERVERS])==30
for bad in ("ssd:20005/vm-20005-disk-0.raw","ssd:10101/vm-10101-disk-0.qcow2",
            "VM_Disk:20005/vm-20005-disk-0.qcow2","/dev/sda"):
    c=dict(sim.cfg[20005]);c["scsi0"]=bad
    raises(lambda:m.owned_disks(20005,c))
c=dict(sim.cfg[20005]);c["unused0"]="ssd:20005/old.raw"
raises(lambda:m.validate_cfg(20005,c),"Unused")
passed("exact VM selection; ISO/cloud-init exclusion; EFI/extra inclusion; RAW/foreign-disk rejection")

sim=Simulation("all_stands_two_workers")
active=0
maximum=0
mutex=threading.Lock()
barrier=threading.Barrier(2)
def work(s):
    global active,maximum
    with mutex:
        active+=1;maximum=max(maximum,active)
    try:
        if s in (1,2):barrier.wait(timeout=5)
        m.prepare_stand(s)
    finally:
        with mutex:active-=1
with cf.ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(work,range(1,16)))
assert maximum==2
assert len(m.state["prepared"])==15 and len(m.state["day0"])==105
assert len(sim.applied)==60 and len(sim.verified)==60
assert all(s=="stopped" for s in sim.power.values())
assert sum(c[:2]==("qemu-img","check") for c in sim.calls)==150
assert sum(c[:2]==("qm","snapshot") for c in sim.calls)==105
assert sum(c[:2]==("qm","start") for c in sim.calls)==120
assert sum(c[:2]==("qm","shutdown") for c in sim.calls)==120
passed("15 stands with 2 workers: 60 guests, power-cycle verification, 150 integrity checks, 105 snapshots")

before=len(sim.calls)
with cf.ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(m.prepare_stand,range(1,16)))
new=sim.calls[before:]
assert not any(c[:2] in (("qm","start"),("qm","snapshot"),("qm","guest")) for c in new)
assert len(m.state["day0"])==105
passed("rerun preserves existing owned Day0 and skips repeated guest changes")

sim=Simulation("foreign_day0")
sim.snaps[20005]["Day0"]={"name":"Day0","description":"existing snapshot"}
raises(lambda:m.prepare_stand(1),"снимки")
assert not any(c[:2] in (("qm","start"),("qm","set"),("qm","guest"),("qm","snapshot")) for c in sim.calls)
passed("foreign snapshot stops a stand before any guest or power changes")

sim=Simulation("guest_failure_resume")
sim.bad_guest=20005
raises(lambda:m.prepare_stand(1),"гостевая проверка")
assert not m.state["day0"] and not m.state["prepared"]
sim.bad_guest=None
m.prepare_stand(1)
assert len(m.state["day0"])==7
passed("guest failure creates no Day0; rerun completes preparation")

sim=Simulation("agent_timeout")
sim.bad_agent=20001
raises(lambda:m.prepare_stand(1),"300")
assert not sim.applied and not m.state["day0"]
passed("unavailable guest agent stops before patch and snapshots")

sim=Simulation("integrity_failure_resume")
sim.bad_disk=20004
raises(lambda:m.prepare_stand(1),"simulated command failure")
assert len(m.state["day0"])==3 and "1" in m.state["prepared"]
sim.bad_disk=None
m.prepare_stand(1)
assert len(m.state["day0"])==7
passed("integrity failure stops snapshot phase; resume preserves already-created Day0")

sim=Simulation("memory")
m.available_memory=lambda:0
raises(lambda:m.prepare_stand(1),"памяти")
assert not any(c[:2]==("qm","start") for c in sim.calls)
passed("memory guard stops before starting guests")

sim=Simulation("async_guest_exec")
sim.async_vm=20001
m.prepare_stand(1)
assert any(c[:3]==("qm","guest","exec-status") for c in sim.calls)
passed("asynchronous QGA result is polled and exit status verified")
sim=Simulation("incomplete_day0")
m.prepare_stand(1)
sim.snaps[20005]["Day0"]["snapstate"]="prepare"
before=len(sim.calls)
raises(lambda:m.prepare_stand(1),"снимки")
assert not any(c[:2] in (("qm","start"),("qm","set"),("qm","guest"),("qm","snapshot")) for c in sim.calls[before:])
passed("incomplete owned Day0 is rejected without overwriting or starting guests")
print(f"ALL {checks} HOST SIMULATIONS PASSED; fixtures: {TESTROOT}")
