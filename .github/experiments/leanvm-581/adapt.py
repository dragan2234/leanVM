"""Modify only the benchmark driver in the disposable upstream checkout."""
from pathlib import Path

path = Path("crates/experimental_prover/keccak_gkr/benches/hash_batch.rs")
original = path.read_text()
old = "    let report = keccak_gkr::batch_bench::run(n, env_usize(\"KECCAK_DEPTH\", 12), LOG_INV_RATE_0, Plan::from_env());"
new = """    parallel::set_num_threads(env_usize("VARIANCE_THREADS", 0));
    let mut plan = Plan::from_env();
    plan.cooldown = std::time::Duration::from_millis(env_usize("VARIANCE_COOLDOWN_MS", 0) as u64);
    let report = keccak_gkr::batch_bench::run(n, env_usize("KECCAK_DEPTH", 12), LOG_INV_RATE_0, plan);
    let spawned = std::fs::read_dir("/proc/self/task")?
        .filter_map(Result::ok)
        .filter_map(|entry| std::fs::read_to_string(entry.path().join("comm")).ok())
        .filter(|name| name.starts_with("parallel-"))
        .count();
    eprintln!("variance_workers configured={} spawned={}", parallel::num_threads(), spawned);"""
if original.count(old) != 1 or original.count("fn main() {") != 1:
    raise SystemExit("Pinned benchmark driver did not match the expected source")
changed = original.replace("fn main() {", "fn main() -> std::io::Result<()> {").replace(old, new)
ending = "    }\n}\n"
assert changed.endswith(ending)
path.write_text(changed[:-len(ending)] + "    }\n    Ok(())\n}\n")
