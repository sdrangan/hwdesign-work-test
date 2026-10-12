from pathlib import Path

root = Path(__file__).resolve().parents[1]
body = root / "maxpool_body_impl.tpp"
lines = body.read_text(encoding="utf-8").splitlines(keepends=True)
start = next(i for i, l in enumerate(lines) if l.startswith("PIXELS:"))
end = next(i for i, l in enumerate(lines) if "// The parked outputs of the last row" in l)
new = (root / "scratch" / "pixels_loop.tpp").read_text(encoding="utf-8")
text = "".join(lines[:start]) + new + "\n" + "".join(lines[end:])
text = text.replace("    const cnt_t npix_m1 = npix - 1;\n",
                    "    const cnt_t npix_m1 = npix - 1;\n    const cnt_t total_m1 = total - 1;\n")
body.write_text(text, encoding="utf-8")
print(f"replaced lines {start + 1}..{end}")
