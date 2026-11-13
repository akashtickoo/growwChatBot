"""Secrets scan (eval.md §12, EV-7-05) over the full git history and the tracked working tree.

Usage: python -m eval.scan_secrets        exit code 1 on any finding

Looks for API-key patterns (Groq, OpenAI-style, GitHub, AWS, Google, Slack), private-key blocks
and filled-in GROQ_API_KEY assignments. Findings print the commit and file, never the secret.
gitleaks, when installed, is a stronger second check (`gitleaks detect`).
"""

import re
import subprocess
import sys

PATTERNS = {
    "groq_key": re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    "openai_style_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    "groq_key_assignment": re.compile(r"GROQ_API_KEY\s*=\s*\"?(?!gsk_\.\.\.)[A-Za-z0-9_]{12,}"),
}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout


def scan_history() -> list[str]:
    """Added lines in every commit on every branch."""
    findings, commit, path = [], "", ""
    for line in _git("log", "--all", "-p", "--no-color", "--format=commit %h").splitlines():
        if line.startswith("commit "):
            commit = line.split()[1]
        elif line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and not line.startswith("+++"):
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"history {commit} {path}: {name}")
    return findings


def scan_worktree() -> list[str]:
    findings = []
    for path in _git("ls-files").splitlines():
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        for name, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"working tree {path}: {name}")
    return findings


def main() -> int:
    findings = sorted(set(scan_history() + scan_worktree()))
    for finding in findings:
        print(f"  - {finding}")
    print("PASS: 0 findings" if not findings else f"FAIL: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
