"""Print a text file as GitHub annotations, so results show on the run's summary page.

usage: python3 .github/annotate.py <notice|warning|error> <title> <file> [--failures-only]
"""

import sys

level, title, path = sys.argv[1], sys.argv[2], sys.argv[3]
failures_only = "--failures-only" in sys.argv[4:]
try:
    text = open(path, encoding="utf-8", errors="replace").read()
except OSError as exc:
    text = f"(could not read {path}: {exc})"
if failures_only and "\n=====" in text:
    text = text[text.index("\n=====") + 1:]  # unittest: keep the failure reports and the summary
text = text.strip() or "(empty)"
CHUNK = 3500
chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)][:10]
for n, chunk in enumerate(chunks, 1):
    msg = chunk.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    suffix = f" ({n}/{len(chunks)})" if len(chunks) > 1 else ""
    print(f"::{level} title={title}{suffix}::{msg}")
