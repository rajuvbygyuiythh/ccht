import os

in_dir = "data/input"
out_dir = "data/output"

print("=== INPUT (triggers) ===")
in_files = {}
for f in sorted(os.listdir(in_dir)):
    if f.endswith(".txt"):
        lines = [l.strip() for l in open(os.path.join(in_dir, f), encoding="utf-8") if l.strip()]
        in_files[f] = len(lines)
        print(f"  {f:30s} {len(lines):5d}")

total_in = sum(in_files.values())
print(f"  {'TOTAL':30s} {total_in:5d}")
print()

print("=== OUTPUT (replies) ===")
out_files = {}
for f in sorted(os.listdir(out_dir)):
    if f.endswith(".txt"):
        lines = [l.strip() for l in open(os.path.join(out_dir, f), encoding="utf-8") if l.strip()]
        out_files[f] = len(lines)
        print(f"  {f:30s} {len(lines):5d}")

total_out = sum(out_files.values())
print(f"  {'TOTAL':30s} {total_out:5d}")
print()

print("=== OUTPUT-ONLY (bot-pool, no triggers) ===")
for f in sorted(out_files.keys()):
    if f not in in_files:
        print(f"  {f:30s} {out_files[f]:5d} replies")

print()
print("=== LOW OUTPUT RATIO (input >> output) ===")
for f in sorted(in_files.keys()):
    if f in out_files and in_files[f] > 0:
        ratio = out_files[f] / in_files[f]
        if ratio < 0.1:
            print(f"  {f:30s} {in_files[f]:5d} in / {out_files[f]:3d} out = {ratio:.2f}")
