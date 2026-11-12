"""Rewrite `name: T = T()` dataclass fields to `field(default_factory=T)` (py3.11+ rejects unhashable defaults)."""
import re
import sys

p = sys.argv[1]
s = open(p).read()
s2, n = re.subn(r"^(\s+\w+\s*:\s*([A-Z]\w*)\s*)=\s*\2\(\)\s*$", r"\1= field(default_factory=\2)", s, flags=re.M)
if n and not re.search(r"^from dataclasses import .*\bfield\b", s2, re.M):
    s2 = "from dataclasses import field\n" + s2
open(p, "w").write(s2)
print(f"patched {n} fields in {p}")
sys.exit(0 if n else 1)
