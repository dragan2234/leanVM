#!/usr/bin/env python3
"""Run one pinned #581 study allocation. --plan prints the schedule without running."""
import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess as sp
import tempfile
import time

SIZES = (13, 18)
COOLDOWNS = (0, 100, 500, 1000, 2000)
MEASURES_HASHES = {"binary","text"}

def read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return None

def capture(argv, env=None, timeout=12):
    try:
        p = sp.run(argv, env=env, timeout=timeout, text=True, capture_output=True)
        return dict(argv=argv, status=p.returncode, stdout=p.stdout, stderr=p.stderr)
    except (OSError, sp.TimeoutExpired) as e:
        return dict(argv=argv, status=None, error=str(e))

def cgroup_paths():
    """Collect readable v2 or conventional v1 CPU paths and their ancestors."""
    paths = set()
    for line in (read("/proc/self/cgroup") or "").splitlines():
        _, controllers, membership = line.split(":", 2)
        member = Path(membership.lstrip("/"))
        bases = [Path("/sys/fs/cgroup")] if not controllers else [
            Path("/sys/fs/cgroup") / c for c in (controllers, *controllers.split(","))
            if c == controllers or c in ("cpu", "cpuacct")
        ]
        for base in bases:
            path = base/member
            for item in (path, *path.parents):
                if item == base or base in item.parents:
                    if item.is_dir():
                        paths.add(item)
    return sorted(paths)

def capacity(groups, affinity):
    quotas = []
    for path in groups:
        data = (read(path/"cpu.max") or "").split()
        if len(data) == 2 and data[0] != "max":
            quotas.append(int(data[0])/int(data[1]))
        q = read(path/"cpu.cfs_quota_us")
        p = read(path/"cpu.cfs_period_us")
        if q and p and int(q)>0 and int(p)>0:
            quotas.append(int(q)/int(p))
    return max(1, math.floor(min([len(affinity), *quotas]))), quotas

def row(experiment, condition, log, binary, *, cooldown=0, threads=0, load=0):
    return dict(experiment=experiment, condition=condition, log_n=log, binary=binary,
                cooldown_ms=cooldown, threads_requested=threads, competing_processes=load)

def balanced(n, rng):
    orders = [i % 2 for i in range(n)]
    rng.shuffle(orders)
    return orders

def cooldown_orders(rng):
    """A randomized Williams design for five treatments across ten blocks."""
    treatments = rng.sample(COOLDOWNS, len(COOLDOWNS))
    template = (0, 1, 4, 2, 3)
    rotations = [tuple(treatments[(i+j)%len(treatments)] for j in template) for i in range(len(treatments))]
    orders = rotations + [r[::-1] for r in rotations]
    rng.shuffle(orders)
    return orders

def schedule(blocks, control_blocks, rng):
    result = []
    orders = {size: balanced(blocks,rng) for size in SIZES}
    for b in range(blocks):
        for size in rng.sample(SIZES,k=2):
            labels = ("A","A_prime") if orders[size][b]==0 else ("A_prime","A")
            result.extend(dict(row("aa",label,size,"pristine"),block=b+1,position=i)
                          for i,label in enumerate(labels,1))

    for b, order in enumerate(balanced(6,rng),1):
        labels = ("pristine","adapter") if order==0 else ("adapter","pristine")
        result.extend(dict(row("bridge",label,18,label),block=b,position=i)
                      for i,label in enumerate(labels,1))

    cooldown = {size: cooldown_orders(rng) for size in SIZES}
    contention = balanced(control_blocks,rng)
    for b in range(control_blocks):
        load_order = (0,1) if contention[b]==0 else (1,0)
        groups = [[row("cooldown",str(d),size,"adapter",cooldown=d) for d in cooldown[size][b]]
                  for size in SIZES]
        groups.append([row("contention",str(load),18,"adapter",load=load) for load in load_order])
        rng.shuffle(groups)
        for group in groups:
            result.extend(dict(r,block=b+1,position=i) for i,r in enumerate(group,1))
    return [dict(r,sequence=i) for i,r in enumerate(result,1)]

def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def snapshot(groups):
    stat = read("/proc/stat")
    return dict(utc=stamp(), cpu=[l for l in stat.splitlines() if l.startswith("cpu")] if stat else None,
                loadavg=read("/proc/loadavg"), pressure=read("/proc/pressure/cpu"),
                cpu_stat={str(p):read(p/"cpu.stat") for p in groups})

def frequency(affinity):
    names = ("scaling_cur_freq", "scaling_governor", "cpuinfo_cur_freq", "scaling_min_freq", "scaling_max_freq")
    return {str(c): {n: read(Path(f"/sys/devices/system/cpu/cpu{c}/cpufreq")/n) for n in names}
            for c in affinity}

def host(groups,affinity, cap, quotas):
    host_files = ["/proc/cpuinfo", "/proc/self/status", "/proc/self/cgroup", "/proc/self/mountinfo",
                  "/proc/meminfo", "/proc/sys/kernel/perf_event_paranoid", "/etc/os-release"]
    group_files = ("cpu.max","cpu.stat","cpuset.cpus.effective","cpu.cfs_quota_us","cpu.cfs_period_us")
    runner_vars = ("ImageOS","ImageVersion","RUNNER_OS","RUNNER_ARCH","GITHUB_RUN_ID","GITHUB_RUN_ATTEMPT",
                   "GITHUB_SHA","UPSTREAM_SHA","RUSTFLAGS","VARIANCE_LABEL")
    return dict(utc=stamp(), files={p:read(p) for p in host_files},
                cgroups={str(p):{n:read(p/n) for n in group_files} for p in groups},
                thermal={str(p):read(p) for p in Path("/sys/class/thermal").glob("thermal_zone*/temp")},
                commands=[capture(x) for x in (["lscpu","-J"],["lscpu","-p=CPU,CORE,SOCKET"],
                                                ["uname","-a"],["rustc","-Vv"])],
                affinity=affinity, capacity=cap, ancestor_cpu_quotas=quotas, frequency=frequency(affinity),
                runner={name:os.environ.get(name) for name in runner_vars})

def clean_env(r):
    env = os.environ.copy()
    for name in ("BENCH_TRACING","LEANVM_TIER","LEANVM_NUM_THREADS","RUST_LOG"):
        env.pop(name,None)
    env.update(KECCAK_N_LOG=str(r["log_n"]),KECCAK_DEPTH="12",BENCH_REPEAT="1",BENCH_COOLDOWN="0",
               VARIANCE_COOLDOWN_MS=str(r.get("cooldown_ms",0)),VARIANCE_THREADS=str(r.get("threads_requested",0)))
    return env

@contextmanager
def background(count):
    tasks=[]
    try:
        for _ in range(count):
            tasks.append(sp.Popen(["python3","-c","while True: pass"],
                                  stdout=sp.DEVNULL,stderr=sp.DEVNULL))
        if tasks:
            time.sleep(.1)
        yield
    finally:
        for task in tasks:
            if task.poll() is None:
                task.terminate()
        for task in tasks:
            task.wait()

def run_one(r, root, groups, hashes):
    argv=[str(root/"bin"/r["binary"]), "--json"]
    with background(r["competing_processes"]), tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        before=snapshot(groups)
        start=time.perf_counter_ns()
        p=sp.Popen(argv, env=clean_env(r), stdout=out, stderr=err)
        _, status, usage=os.wait4(p.pid,0)
        p.returncode=os.waitstatus_to_exitcode(status)
        wall_ns=time.perf_counter_ns()-start
        after=snapshot(groups)
        out.seek(0)
        err.seek(0)
        stdout=out.read().decode(errors="replace")
        stderr=err.read().decode(errors="replace")
    worker = re.search(r"variance_workers configured=(\d+) spawned=(\d+)",stderr)
    r.update(status=p.returncode, wall_ns=wall_ns, before=before, after=after, stdout=stdout, stderr=stderr,
             binary_hashes=hashes[r["binary"]], tier="standalone experimental build",
             configured_workers=int(worker[1]) if worker else None,
             actual_workers=1+int(worker[2]) if worker else None,
             usage={name:getattr(usage,"ru_"+name) for name in (
                 "utime","stime","maxrss","nvcsw","nivcsw","majflt","minflt")})
    if p.returncode==0:
        try:
            value=json.loads(stdout)[f"keccak-hash-batch-{1<<r['log_n']}"]["latency"]["value"]
            if not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
                raise ValueError("nonpositive or nonfinite latency")
            r["proving_ns"]=value
            if r["binary"]=="adapter" and (not worker or r["actual_workers"]!=r["configured_workers"]):
                raise ValueError("worker count did not match pool configuration")
        except (ValueError,KeyError,TypeError) as e:
            r["error"]=str(e)
    return r

def probe_workers(root,size):
    """Observe pristine worker names outside measurement to avoid sampling overhead."""
    names=[]
    with tempfile.TemporaryFile() as out:
        p=sp.Popen([str(root/"bin"/"pristine"),"--json"],
                   env=clean_env({"log_n":size}),stdout=out,stderr=out)
        try:
            while p.poll() is None:
                task_dir=Path(f"/proc/{p.pid}/task")
                try:
                    sample=[n for t in task_dir.iterdir() if (n:=read(t/"comm")) and n.startswith("parallel-")]
                    if len(sample)>len(names):
                        names=sample
                except OSError:
                    pass
                time.sleep(.04)
            out.seek(0)
            return dict(log_n=size, named_workers=sorted(names), workers_including_dispatcher=1+len(names),
                        exit=p.returncode, output=out.read().decode(errors="replace"))
        finally:
            if p.poll() is None:
                p.terminate()
                p.wait()

def save(path, data):
    path.write_text(json.dumps(data,indent=2)+"\n")

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--plan",action="store_true",help="print a synthetic schedule only")
    args=parser.parse_args()
    stage=os.environ["VARIANCE_STAGE"]
    blocks=int(os.environ["VARIANCE_BLOCKS"])
    controls=int(os.environ["VARIANCE_CONTROL_BLOCKS"])
    window=os.environ["VARIANCE_WINDOW"]
    session=int(os.environ["VARIANCE_SESSION"])
    arch=os.environ["VARIANCE_ARCH"]
    allowed={"first":(1,2),"second":(3,4)}
    if ((stage, blocks, controls)!=("full",30,10) or session not in allowed.get(window,())
            or arch not in ("x86-64","arm64")):
        raise SystemExit("Invalid study architecture, window, allocation, or block counts.")
    run_id=os.environ.get("GITHUB_RUN_ID","local")
    attempt=os.environ.get("GITHUB_RUN_ATTEMPT","1")
    if attempt!="1":
        raise SystemExit("Use the first attempt only. Any replacement allocation needs a new review.")
    key=(stage,window,session,arch,run_id)
    seed=int.from_bytes(hashlib.sha256(repr(key).encode()).digest()[:8],"big")
    rows=schedule(blocks,controls,random.Random(seed))
    if args.plan:
        print(json.dumps(rows))
        return
    root=Path(os.environ["VARIANCE_RESULTS"])
    affinity=sorted(os.sched_getaffinity(0))
    groups=cgroup_paths()
    cap,quotas=capacity(groups,affinity)
    hashes={}
    for file,name in (("binary-sha256.txt","binary"),("text-sha256.txt","text")):
        for line in (root/file).read_text().splitlines():
            value,location=line.split(maxsplit=1)
            label=Path(location).name.split(".")[0]
            hashes.setdefault(label,{})[name]=value
    assert all(set(value)==MEASURES_HASHES for value in hashes.values())
    save(root/"host-before.json",host(groups,affinity,cap,quotas))
    save(root/"design.json",dict(stage=stage,seed=seed,session=session,arch=arch,run_id=run_id,attempt=attempt,
                                blocks=blocks,control_blocks=controls,window=window,capacity=cap,
                                hashes=hashes,rows=rows,first_snapshot=snapshot(groups)))
    try:
        with (root/"runs.jsonl").open("w",buffering=1) as output:
            for r in rows:
                r["frequency_before_process"]=frequency(affinity)
                r.update(session=session,window=window,arch=arch,run_id=run_id,attempt=attempt,seed=seed)
                item=run_one(r,root,groups,hashes)
                output.write(json.dumps(item)+"\n")
                if item["status"] or item.get("error"):
                    raise RuntimeError(f"failed benchmark row {item['sequence']}, see runs.jsonl")
                print(f"{item['sequence']}/{len(rows)} {item['experiment']} {item['log_n']} {item['condition']} {item['proving_ns']/1e6:.3f}ms",flush=True)
        save(root/"pristine-workers.json",[probe_workers(root,size) for size in SIZES])
    finally:
        save(root/"host-after.json",host(groups,affinity,cap,quotas))
        save(root/"last-snapshot.json",snapshot(groups))

if __name__=="__main__":
    main()
