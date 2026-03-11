#!/usr/bin/env python3
"""Read filled-in keys from SETUP_KEYS.md and write them into .env.

Usage:
    python scripts/setup_env.py
    # or: make setup-env
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP_KEYS = ROOT / "SETUP_KEYS.md"
ENV_EXAMPLE = ROOT / ".env.example"
ENV_FILE = ROOT / ".env"

# Keys we expect in SETUP_KEYS.md (order preserved)
EXPECTED_KEYS = [
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_REGION",
    "ALCHEMY_API_KEY",
    "TAVILY_API_KEY",
]


def parse_keys(path: Path) -> dict[str, str]:
    """Extract KEY=VALUE pairs from SETUP_KEYS.md."""
    text = path.read_text()
    found: dict[str, str] = {}
    for match in re.finditer(r"^([A-Z_]+)=(.+)$", text, re.MULTILINE):
        key, value = match.group(1), match.group(2).strip()
        if key in EXPECTED_KEYS and value:
            found[key] = value
    return found


def build_env(keys: dict[str, str]) -> str:
    """Read .env.example and substitute with real keys."""
    lines = ENV_EXAMPLE.read_text().splitlines()
    out: list[str] = []
    for line in lines:
        replaced = False
        for key, value in keys.items():
            # Match lines like KEY=placeholder or KEY=...
            pattern = rf"^{re.escape(key)}=.*$"
            if re.match(pattern, line):
                out.append(f"{key}={value}")
                replaced = True
                break
        if not replaced:
            out.append(line)
    return "\n".join(out) + "\n"


def main() -> None:
    if not SETUP_KEYS.exists():
        print(f"❌ {SETUP_KEYS} not found. Fill in your keys there first.")
        sys.exit(1)

    keys = parse_keys(SETUP_KEYS)

    # Report status
    missing = [k for k in EXPECTED_KEYS if k not in keys]
    filled = [k for k in EXPECTED_KEYS if k in keys]

    if filled:
        print("✅ Found keys:")
        for k in filled:
            # Mask the value, show first 4 + last 4 chars
            v = keys[k]
            masked = v[:4] + "…" + v[-4:] if len(v) > 12 else "****"
            print(f"   {k}={masked}")

    if missing:
        print(f"\n⚠️  Missing keys (will keep placeholder from .env.example):")
        for k in missing:
            print(f"   {k}")

    # Write .env
    env_content = build_env(keys)
    ENV_FILE.write_text(env_content)
    print(f"\n✅ Wrote {ENV_FILE}")

    if missing:
        print(f"   {len(missing)} key(s) still need filling — edit .env directly or update SETUP_KEYS.md and re-run.")
    else:
        print("   All keys set. Run `make db-init && make run` to verify.")


if __name__ == "__main__":
    main()
