#!/usr/bin/env python3
"""
guard_secrets.py

Fails the build if anything credential-shaped is about to become part of a
PUBLIC git repository. Used two ways:

  python3 guard_secrets.py --staged     # pre-commit hook: only what's being committed
  python3 guard_secrets.py              # CI / manual: every tracked file at HEAD

WHY THIS EXISTS
    A GitHub Secret cannot be un-leaked: it is encrypted and never rendered.
    A credential committed to a PUBLIC repo is the opposite - it is served to
    every visitor from a cache within minutes, and stays in git history
    permanently even after the file is deleted. Rewriting history does not
    reliably retract it, because unreachable commits remain fetchable by SHA
    and any mirror or scraper that already read it still has a copy.

    So the real defence is structural: real values never go into a tracked
    file. This script enforces that, rather than trusting everyone to remember.

WHAT IT CHECKS
  1. Forbidden filenames that must never be tracked (the filled-in configs,
     credentials/, tokens, keys).
  2. Real-looking email addresses in tracked files.
  3. password/passwd assignments whose value is not an obvious placeholder.
  4. Credential-shaped tokens (GitHub, Google OAuth, Google API keys).

Placeholders are deliberately tolerated, so the *.yaml.template files - which
exist precisely to hold example structure - pass cleanly. The bar is set to
catch a real address or a real password, not to enforce a style guide.

This is a safety net, not a guarantee. It cannot tell that
"correcthorsebatterystaple" is a throwaway. The authoritative rule remains:
real credentials go in the untracked config copy and in GitHub Secrets, never
in a committed file.
"""

import argparse
import os
import re
import subprocess
import sys

# --- Allowlists ----------------------------------------------------------

# Domains that legitimately appear in code, docs, and configs.
SAFE_DOMAINS = {
    "example.com", "example.org", "example.net",   # RFC 2606 reserved
    "noreply.github.com", "github.com", "githubusercontent.com",
    "mega.nz", "googleapis.com", "google.com", "goo.gl",
    "gnu.org", "rclone.org", "python.org", "peps.python.org",
    "opensource.org", "wgid.org", "readthedocs.io", "pypi.org",
}

# A local-part containing any of these is a documented example, not a person.
PLACEHOLDER_MARKERS = (
    "{n}", "your", "example", "replace_me", "replaceme", "changeme",
    "change_me", "xxx", "<", "yourbase", "your-email", "email",
)

# Password values that are documentation, not credentials.
SAFE_PASSWORDS = {
    "replaced", "", "none", "null", "true", "false", "...",
    "***", "[redacted]", "[redacted-email]", "redacted",
    "your-password", "your-shared-password", "shared", "shared-secret",
    "own", "own2", "explicit", "fallback", "p", "p1", "p2",
    "os.getenv", "config", "env", "one", "two", "megapass", "password",
}

# Files that must never be tracked.
FORBIDDEN_EXACT = {
    "config.yaml", "config_drive_source.yaml", "rclone.conf",
    "id_rsa", "id_ed25519", ".env", "credentials.json", "token.json",
}
FORBIDDEN_DIRS = ("credentials/", ".env", "secrets/")
FORBIDDEN_SUFFIX = (".pem", ".key", ".p12", ".pfx", ".keystore")

# --- Patterns ------------------------------------------------------------

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)")
TOKEN = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}"            # GitHub PAT / token
    r"|1//[A-Za-z0-9_\-]{25,}"              # Google OAuth refresh token
    r"|GOCSPX-[A-Za-z0-9_\-]{10,}"          # Google OAuth client secret
    r"|AIza[0-9A-Za-z_\-]{30,}"             # Google API key
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
PASSWORD = re.compile(
    r"(?i)\b(password|passwd|pwd|passphrase)\b\s*[:=]\s*[\"']?([^\s\"',#\]\)}]{3,})"
)
# A `password: <key>` line where the "value" is really the next YAML key.
PLACEHOLDER_SUFFIX = ("accounts:", "channels:", "file:", "_file:", "s:")

TEXT_SUFFIXES = (".md", ".py", ".yml", ".yaml", ".json", ".txt", ".cfg", ".ini",
                 ".toml", ".sh", ".ps1", ".template", ".example", "")


def run(*args):
    return subprocess.run(["git"] + list(args), capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def tracked_files():
    return [p for p in run("ls-files").splitlines() if p.strip()]


def staged_blobs():
    """(path, content) for each file staged for commit, or for a deleted path
    an empty string."""
    out = []
    for line in run("diff", "--cached", "--name-status").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        if status.startswith("D"):
            out.append((path, ""))
            continue
        blob = subprocess.run(["git", "show", ":" + path], capture_output=True,
                              text=True, encoding="utf-8", errors="replace").stdout
        out.append((path, blob))
    return out


def head_blobs():
    out = []
    for path in tracked_files():
        blob = subprocess.run(["git", "show", "HEAD:" + path], capture_output=True,
                              text=True, encoding="utf-8", errors="replace").stdout
        out.append((path, blob))
    return out


def is_placeholder_email(local):
    low = local.lower()
    return any(m in low for m in PLACEHOLDER_MARKERS)


def scan_text(path, text, findings):
    if not text:
        return

    base = path.replace("\\", "/")
    name = base.rsplit("/", 1)[-1]

    # 1. forbidden filenames
    if base in FORBIDDEN_EXACT:
        findings.append((path, "FORBIDDEN FILE",
                         "this file holds real credentials and must never be committed"))
    if any(base.startswith(d) or ("/" + d) in base for d in FORBIDDEN_DIRS):
        findings.append((path, "FORBIDDEN FILE",
                         "credential directory must never be committed"))
    if name.endswith(FORBIDDEN_SUFFIX):
        findings.append((path, "FORBIDDEN FILE", "key/certificate file must never be committed"))
    if name.endswith(".json") and "token" in name.lower():
        findings.append((path, "FORBIDDEN FILE", "token file must never be committed"))

    # skip scanning the guard's own source: it necessarily contains the
    # placeholder vocabulary it treats as safe.
    if base.endswith("guard_secrets.py"):
        return

    # 2. emails
    for m in EMAIL.finditer(text):
        domain = m.group(1).lower()
        local = m.group(0).split("@")[0]
        # suffix match, so subdomains of an allowlisted domain pass
        # (actions@users.noreply.github.com -> noreply.github.com)
        if any(domain == d or domain.endswith("." + d) for d in SAFE_DOMAINS):
            continue
        if is_placeholder_email(local):
            continue
        line = text[:m.start()].count("\n") + 1
        findings.append((path, f"EMAIL (line {line})",
                         f"{m.group(0)} - a real address here is public forever"))

    # 3. passwords
    for m in PASSWORD.finditer(text):
        kw, val = m.group(1), m.group(2)
        low = val.lower()
        if low in SAFE_PASSWORDS or "redacted" in low:
            continue
        if val.endswith(PLACEHOLDER_SUFFIX):
            continue
        if any(p in low for p in ("replace", "your", "example", "changeme", "xxx")):
            continue
        # a bare key name, e.g. `password: password` in a f-string example
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", val) and val.lower() in (
                "password", "passwd", "pwd", "self", "cred", "creds", "token"):
            continue
        line = text[:m.start()].count("\n") + 1
        findings.append((path, f"PASSWORD (line {line})",
                         f"{kw}={val} - looks like a real value"))

    # 4. tokens
    for m in TOKEN.finditer(text):
        line = text[:m.start()].count("\n") + 1
        findings.append((path, f"TOKEN (line {line})",
                         m.group(0)[:20] + "... - credential-shaped string"))


def mask_file(path):
    """Print a file with every credential value masked, so its structure can be
    shared (in a chat, an issue, a bug report) without exposing the secrets.

    This is the safe way to ask for help with a config: run this, paste the
    output, and every password, token, client secret, and account address comes
    out as [redacted] while the keys, nesting, and non-secret values such as
    folder_id stay readable.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        print(f"No such file: {path}")
        return 1

    # Value keys that are always secret, matched on substring so that prefixed
    # variants (mega_accounts_default_password) are caught too.
    SECRET_KEY_PARTS = ("password", "passwd", "pwd", "passphrase", "token",
                        "client_secret", "api_key", "secret")
    # Keys whose value is an account address rather than a credential.
    EMAIL_KEYS = ("email", "email_pattern", "default_email")

    # Looser than EMAIL: allows {n} so a pattern like realbase+{n}@gmail.com
    # still matches and still gets masked.
    loose_email = re.compile(r"[A-Za-z0-9._%+\-{}\[\]]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")

    # Suffixes whose values are locations or identifiers rather than secrets.
    # client_secret_file and token_file hold paths; seeing them is the whole
    # point when a config is misconfigured.
    PATH_SUFFIXES = ("_file", "_path", "_dir", "_id")

    out = []
    for line in text.splitlines():
        m = re.match(r"^(\s*(?:-?\s*)?)([A-Za-z0-9_.\-]+)(\s*:\s*)(.*)$", line)
        if m and m.group(4).strip():
            indent, key, sep, val = m.group(1), m.group(2), m.group(3), m.group(4)
            low = key.lower()
            is_path = low.endswith(PATH_SUFFIXES)
            if any(p in low for p in SECRET_KEY_PARTS) and not is_path:
                line = f"{indent}{key}{sep}[redacted]"
            elif any(low == k or low.endswith("_" + k) for k in EMAIL_KEYS) and not is_path:
                line = f"{indent}{key}{sep}[redacted-email]"
            else:
                line = TOKEN.sub("[redacted-token]", loose_email.sub("[redacted-email]", line))
        out.append(line)

    print(f"--- {os.path.basename(path)} (secrets masked) ---")
    print("\n".join(out))
    print("--- end ---")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Block credentials entering a public repo.")
    ap.add_argument("--staged", action="store_true",
                    help="only scan what is staged for commit (pre-commit hook)")
    ap.add_argument("--show", metavar="FILE", nargs="?",
                    help="print a config file with all secret values masked, "
                         "so it is safe to share")
    args = ap.parse_args()

    if args.show:
        return mask_file(args.show)

    blobs = staged_blobs() if args.staged else head_blobs()
    if not blobs:
        print("guard_secrets: nothing to scan.")
        return 0

    findings = []
    for path, text in blobs:
        scan_text(path, text, findings)

    scope = "staged changes" if args.staged else "tracked files at HEAD"
    if not findings:
        print(f"guard_secrets: OK - {len(blobs)} {scope} clean.")
        return 0

    print(f"guard_secrets: BLOCKED - {len(findings)} problem(s) in {scope}\n")
    seen = set()
    for path, kind, detail in findings:
        key = (path, kind, detail)
        if key in seen:
            continue
        seen.add(key)
        print(f"  {path}")
        print(f"      {kind}: {detail}")
    print(
        "\n"
        "This repository is PUBLIC. Anything committed here is world-readable\n"
        "within minutes and cannot be recalled from git history.\n"
        "\n"
        "What to do instead:\n"
        "  - Real MEGA/Google values go in the UNTRACKED config copy\n"
        "    (config.yaml / config_drive_source.yaml - already gitignored),\n"
        "    never in the *.yaml.template files, which ARE tracked.\n"
        "  - Then paste that file's contents into the matching GitHub Secret\n"
        "    (Settings -> Secrets and variables -> Actions). Secrets are\n"
        "    encrypted and never rendered in logs or the UI.\n"
        "  - Real account addresses should appear only as an `alias`.\n"
        "\n"
        "If something real is already committed, removing the file is not\n"
        "enough - treat the credential as compromised and rotate it.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
