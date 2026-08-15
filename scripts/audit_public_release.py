from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DENIED_PREFIXES = (
    "data/store/",
    "data/sources/uploads/",
    "data/vault/",
    "data/runtime/",
    "output/",
    ".dev-logs/",
)
DENIED_NAMES = {".env", ".dev-run.json"}
DENIED_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm",
    ".pem", ".p12", ".pfx", ".key",
}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "Google API key": re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "Alibaba access key": re.compile(r"\bLTAI[A-Za-z0-9]{16,}\b"),
}


def git_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return sorted({line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()})


def private_denylist() -> list[str]:
    value = os.getenv("PUBLIC_RELEASE_DENYLIST_FILE", "").strip()
    if not value:
        return []
    return [line.strip() for line in Path(value).read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]


def main() -> int:
    errors: list[str] = []
    paths = git_paths()
    deny_terms = private_denylist()
    for relative in paths:
        lower = relative.lower()
        path = ROOT / relative
        if Path(relative).name.lower() in DENIED_NAMES:
            errors.append(f"denied filename: {relative}")
        if lower.startswith(DENIED_PREFIXES):
            errors.append(f"runtime/private path: {relative}")
        if any(lower.endswith(suffix) for suffix in DENIED_SUFFIXES):
            errors.append(f"denied file type: {relative}")
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                errors.append(f"possible {label}: {relative}")
        for term in deny_terms:
            if term in content or term in relative:
                errors.append(f"private denylist term found in: {relative}")
    if errors:
        print("PUBLIC RELEASE AUDIT FAILED")
        for error in sorted(set(errors)):
            print(f"- {error}")
        return 1
    print(f"PUBLIC RELEASE AUDIT PASSED ({len(paths)} candidate files checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
