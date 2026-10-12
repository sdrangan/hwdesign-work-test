from pathlib import Path

root = Path(__file__).resolve().parents[1]
body = root / "maxpool_body_impl.tpp"
lines = body.read_text(encoding="utf-8").splitlines(keepends=True)
start = next(i for i, l in enumerate(lines) if l.startswith("// One COMPUTE whose header"))
end = next(i for i, l in enumerate(lines) if l.startswith("// The kernel: one activation"))
new = (root / "scratch" / "compute.tpp").read_text(encoding="utf-8")
body.write_text("".join(lines[:start]) + new + "".join(lines[end:]), encoding="utf-8")
print(f"replaced lines {start + 1}..{end}")
