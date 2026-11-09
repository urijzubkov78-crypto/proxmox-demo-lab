#!/usr/bin/env python3
"""Read-only inventory of our deployed DEMO_2; no guest commands or mutations."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("finalizer", ROOT / "finalize_demo2.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


def main():
    problems = []
    total = disks = day0 = stopped = iso = 0
    for stand in range(1, 16):
        expected = f.vmids(stand)
        try:
            pool = f.jget(f"/pools/DEMO_2_S{stand}")
            members = pool.get("members", [])
            if (len(members) != 7 or
                sorted(int(m.get("vmid", -1)) for m in members) != expected or
                any(m.get("type") != "qemu" or m.get("node") != "pve" for m in members)):
                raise RuntimeError("состав пула отличается от ожидаемого")
            for vid in expected:
                cfg = f.config(vid)
                own = f.validate_cfg(vid, cfg)
                for volume in own.values():
                    path = Path(f.text("pvesm", "path", volume))
                    if path.resolve().parent != Path(f"/mnt/ssd/images/{vid}") or not path.is_file():
                        raise RuntimeError(f"VM {vid}: неожиданный или отсутствующий файл диска")
                disks += len(own)
                total += 1
                stopped += f.status(vid) == "stopped"
                snap = f.snapshots(vid).get("Day0", {})
                if snap.get("name") == "Day0" and not snap.get("snapstate"):
                    day0 += 1
                else:
                    problems.append(f"VM {vid}: нет завершённого Day0")
                if vid % 100 in f.SERVERS:
                    if cfg.get("ide2", "").split(",")[0] == f.ISO:
                        iso += 1
                    else:
                        problems.append(f"VM {vid}: не подключён Additional.iso")
            print(f"DEMO_2_S{stand}: 7 ВМ, {expected[0]}-{expected[-1]}")
        except Exception as exc:
            problems.append(f"DEMO_2_S{stand}: {exc}")
    print(f"ВМ {total}/105; рабочих QCOW2 {disks}/150; Day0 {day0}/105; ISO {iso}/30; выключено {stopped}/105")
    print("Гостевые APT и автомонтирование эта проверка не проверяет. ВМ не изменялись.")
    for problem in problems:
        print("ОШИБКА: " + problem, file=sys.stderr)
    return 1 if problems or (total, disks, day0, iso) != (105, 150, 105, 30) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("СТОП: " + str(exc), file=sys.stderr)
        sys.exit(1)
