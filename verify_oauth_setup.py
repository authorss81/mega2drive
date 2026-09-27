#!/usr/bin/env python3
"""
verify_oauth_setup.py

Checks that each destination is wired to the right Google service and the right
account, WITHOUT printing any secret.

WHY THIS EXISTS
    The client_secret_*.json files cannot answer "is Drive pointed at Drive and
    the channel at YouTube?" - an OAuth client is just the app's identity and
    contains no service information whatsoever. The same client legitimately
    serves both. What actually decides it is:
      * the SCOPES the token was granted, and
      * WHICH Google account performed the authorization.
    So this inspects the token files, which is where that information lives.

    It reports for each destination: which account authorized it, which scopes
    it actually holds, whether the token matches its client, and whether a
    refresh token is present. Secrets are never printed - only presence, prefix,
    and counts.

Run it before adding the GitHub secrets, and again any time a token seems not
to work.
"""

import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML not installed. Run: pip install -r requirements.txt")

BASE_DIR = Path(__file__).resolve().parent

# The scopes each destination is supposed to hold. Kept in sync with
# mega_to_youtube.py / drive_to_mega_youtube.py - if you change the scopes
# there, change them here too.
EXPECTED = {
    "youtube": [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube",
    ],
    "drive": [
        "https://www.googleapis.com/auth/drive.file",
    ],
    "drive_source": [
        "https://www.googleapis.com/auth/drive.readonly",
    ],
}

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def resolve(p):
    """Same rules as the pipeline scripts: absolute wins, else relative to
    MEGA2DRIVE_CREDENTIALS, else relative to the repo."""
    path = Path(p)
    if path.is_absolute():
        return path
    creds = os.environ.get("MEGA2DRIVE_CREDENTIALS") or ""
    if creds:
        return Path(creds) / path
    return BASE_DIR / path


def jwt_claims(token):
    """Decode an id_token payload WITHOUT verifying it. This is only to read
    the 'email' claim so you can confirm the right account authorized - it is
    not a security decision and no signature check is needed for that."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return {}


def check(label, ok, detail=""):
    print(f"    [{GREEN}OK{RESET}]   {label}" if ok else f"    [{RED}FAIL{RESET}] {label}")
    if detail:
        print(f"           {DIM}{detail}{RESET}")
    return ok


def info(label, detail=""):
    print(f"    [{YELLOW}--{RESET}]   {label}")
    if detail:
        print(f"           {DIM}{detail}{RESET}")


def report(name, service, cfg_entry, problems):
    print(f"\n{'-' * 74}")
    print(f"  {name}  ->  {service.upper()}")
    print(f"{'-' * 74}")

    secret_path = resolve(cfg_entry["client_secret_file"])
    token_path = resolve(cfg_entry["token_file"])

    # --- client secret file ---
    if not secret_path.exists():
        problems.append(f"{name}: client secret missing at {secret_path}")
        check("client_secret file present", False, str(secret_path))
        return
    try:
        cs = json.loads(secret_path.read_text(encoding="utf-8"))
    except Exception as e:
        problems.append(f"{name}: client secret is not valid JSON ({e})")
        check("client_secret is valid JSON", False, str(e))
        return

    kind = "installed" if "installed" in cs else ("web" if "web" in cs else None)
    check("client_secret file present and parseable", True, str(secret_path))
    check("client type is 'installed' (Desktop app)", kind == "installed",
          f"found '{kind}'" + ("" if kind == "installed" else "  <- web clients cannot use localhost redirect"))
    body = cs.get(kind) or {}
    check("has client_id", bool(body.get("client_id")),
          (body.get("client_id", "")[:28] + "...") if body.get("client_id") else "MISSING")
    check("has client_secret", bool(body.get("client_secret")), "present (value not shown)")

    # --- token file ---
    if not token_path.exists():
        problems.append(f"{name}: no token yet - run --authorize-only")
        info("token file", f"absent: {token_path}")
        info("next step", "python mega_to_youtube.py --authorize-only   (or drive_to_mega_youtube.py)")
        return
    try:
        tok = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception as e:
        problems.append(f"{name}: token is not valid JSON ({e})")
        check("token is valid JSON", False, str(e))
        return

    check("token file present and parseable", True, str(token_path))

    # which account authorized it
    claims = jwt_claims(tok.get("id_token", ""))
    email = claims.get("email") or claims.get("email_verified") and claims.get("email")
    if email:
        print(f"    [{GREEN}OK{RESET}]   authorized by : {email}")
    else:
        info("authorized by", "not readable offline (no id_token claim); "
                              "check the Google account you signed in with")

    # scopes actually granted
    granted = set(tok.get("scopes") or [])
    wanted = EXPECTED.get(service, [])
    if granted:
        print(f"    [{GREEN}OK{RESET}]   {len(granted)} scope(s) granted:")
        for s in sorted(granted):
            mark = f"{GREEN}expected{RESET}" if s in wanted else f"{YELLOW}extra{RESET}"
            print(f"           {DIM}{s}   [{mark}]{RESET}")
        missing = [s for s in wanted if s not in granted]
        if missing:
            problems.append(f"{name}: token missing expected scope(s) {missing}")
            check("all expected scopes present", False, "missing: " + ", ".join(missing))
        else:
            check("all expected scopes present", True,
                  "matches what the script will request")
    else:
        problems.append(f"{name}: token has no scopes recorded - re-authorize")
        check("scopes recorded", False, "token file has no 'scopes' field")

    # token must belong to this client
    if body.get("client_id") and tok.get("client_id"):
        same = body["client_id"] == tok["client_id"]
        check("token matches this client_secret", same,
              "client_id agrees" if same else "token was issued by a DIFFERENT client - re-authorize")
        if not same:
            problems.append(f"{name}: token client_id does not match its client secret")

    # refresh token = can this renew itself?
    check("has refresh_token (auto-renews)", bool(tok.get("refresh_token")),
          "present" if tok.get("refresh_token") else
          "MISSING - token will expire and NOT renew; re-authorize")

    exp = tok.get("expiry")
    if exp:
        try:
            when = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            print(f"           {DIM}access token expires: {when:%Y-%m-%d %H:%M} local{RESET}")
        except Exception:
            pass


def main():
    cfg_path = Path(os.environ.get("MEGA2DRIVE_CONFIG") or (BASE_DIR / "config.yaml"))
    print(f"config: {cfg_path}")
    if not cfg_path.exists():
        sys.exit(f"\nConfig not found: {cfg_path}\n"
                 f"Set MEGA2DRIVE_CONFIG, or copy config.yaml.template to config.yaml.")
    config = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    problems = []

    print("\n" + "=" * 74)
    print("  YOUTUBE CHANNELS")
    print("=" * 74)
    channels = config.get("youtube_channels") or []
    if not channels:
        problems.append("no youtube_channels configured")
        print("  none configured")
    for ch in channels:
        report(ch.get("name", "channel"), "youtube", ch, problems)

    print("\n" + "=" * 74)
    print("  GOOGLE DRIVE (Pipeline A backup destination)")
    print("=" * 74)
    gd = config.get("google_drive") or {}
    if not gd:
        print("  no google_drive section")
    elif not gd.get("enabled"):
        info("google_drive.enabled is FALSE", "no Drive copies will be made")
        problems.append("google_drive.enabled is false - Drive destination is off")
    else:
        report("google_drive", "drive", gd, problems)

    print("\n" + "=" * 74)
    if problems:
        print(f"  {RED}{len(problems)} PROBLEM(S){RESET}")
        for p in problems:
            print(f"    - {p}")
        print()
    else:
        print(f"  {GREEN}All checks passed. Ready for the GitHub secrets.{RESET}")
    print("=" * 74)
    print()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
