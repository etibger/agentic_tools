"""Check publication inputs for imported archives and common sensitive-data patterns."""
import hashlib
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_PATHS = (
    "reference/confluence/",
    "docs/assets/source/",
    "scripts/export_reference.py",
)
# Exact-content allowlist for reviewed assets. The figure is metadata-stripped.
# The original workflow PDF was explicitly approved for public inclusion.
# Changed asset bytes require another review before updating these hashes.
REVIEWED_ASSETS = {
    "docs/assets/agent-communication.png": "7a0d735ccfbc92964d6c4e6917c8d2f2832df531f097f00e276a7ee6dde90f7c",
    "docs/assets/coordinated-multi-agent-workflow.pdf": "fbd36d32c32839e7221b8669614edeb4849e415b650b18e9039f877154bf0982",
}
PATTERNS = (
    ("internal service URL", re.compile(r"https?://(?:confluence|jira|gitlab)\.[A-Za-z0-9.-]+", re.I)),
    ("GitHub credential", re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("API credential", re.compile(r"sk-[A-Za-z0-9_-]{24,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)

def main():
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, check=True, capture_output=True,
    )
    findings = []
    files = sorted(set(result.stdout.decode().split("\0")) - {""})
    for name in files:
        if any(name == prefix or name.startswith(prefix) for prefix in BLOCKED_PATHS):
            findings.append((name, "blocked imported archive path"))
        path = ROOT / name
        if not path.is_file():
            continue
        content = path.read_bytes()
        if name in REVIEWED_ASSETS:
            if hashlib.sha256(content).hexdigest() != REVIEWED_ASSETS[name]:
                findings.append((name, "reviewed asset changed; disclosure review required"))
            continue
        if b"\0" in content:
            findings.append((name, "binary file requires separate disclosure review"))
            continue
        text = content.decode("utf-8", errors="replace")
        for label, pattern in PATTERNS:
            if pattern.search(text):
                findings.append((name, label))
    if findings:
        for name, label in findings:
            print(f"{name}: {label}")
        raise SystemExit(1)
    print(f"Publication inputs checked: {len(files)} files; no blocked imports or common credential patterns.")
    print("This is a bounded pattern check; semantic review is also required.")

if __name__ == "__main__":
    main()
