#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$VARIANCE_RESULTS/bin"
experiment_dir="$PWD/control/.github/experiments/leanvm-581"
cd upstream
test "$(git rev-parse HEAD)" = "$UPSTREAM_SHA"
{
  git rev-parse HEAD
  rustc -Vv
  cargo -V
  printf 'RUSTFLAGS=%s\n' "$RUSTFLAGS"
  rustc --print cfg $RUSTFLAGS
  sha256sum Cargo.lock
} > "$VARIANCE_RESULTS/build-metadata.txt"

unset BENCH_TRACING LEANVM_TIER LEANVM_NUM_THREADS RUSTC_WRAPPER RUSTC_WORKSPACE_WRAPPER
build() {
  local label=$1 executable
  cargo bench --locked --no-run --message-format=json \
    -p experimental_keccak_gkr --bench hash_batch > "$VARIANCE_RESULTS/build-$label.jsonl"
  executable=$(jq -er -s '
    [.[] | select(.reason=="compiler-artifact" and .target.name=="hash_batch" and .executable!=null)
         | .executable] | if length==1 then .[0] else error("expected one benchmark binary") end
  ' "$VARIANCE_RESULTS/build-$label.jsonl")
  cp "$executable" "$VARIANCE_RESULTS/bin/$label"
  sha256sum "$VARIANCE_RESULTS/bin/$label" >> "$VARIANCE_RESULTS/binary-sha256.txt"
  objcopy -O binary --only-section=.text "$VARIANCE_RESULTS/bin/$label" "$VARIANCE_RESULTS/$label.text"
  test -s "$VARIANCE_RESULTS/$label.text"
  sha256sum "$VARIANCE_RESULTS/$label.text" >> "$VARIANCE_RESULTS/text-sha256.txt"
  rm "$VARIANCE_RESULTS/$label.text"
}
build pristine
if [[ "$VARIANCE_STAGE" != aa ]]; then
  python3 "$experiment_dir/adapt.py"
  git diff -- crates/experimental_prover/keccak_gkr/benches/hash_batch.rs > "$VARIANCE_RESULTS/bench-driver.diff"
  test -s "$VARIANCE_RESULTS/bench-driver.diff"
  build adapter
fi
