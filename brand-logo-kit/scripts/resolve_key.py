#!/usr/bin/env python3
"""Discover, show, set, or clear the API key used by brand-logo-kit.

No key ships with this skill. Running with no arguments auto-discovers a key
from the environment or another installed skill and caches it (outside the repo,
at ~/.brand-logo-kit/config.json) for later runs.

Usage:
    python3 resolve_key.py                        # auto-discover + cache, print status
    python3 resolve_key.py --provider google      # only accept a Google key
    python3 resolve_key.py --provider openrouter  # only accept an OpenRouter key
    python3 resolve_key.py --show                 # print cached config (key masked)
    python3 resolve_key.py --set <KEY>            # cache a key manually
    python3 resolve_key.py --clear                # forget the cached key
"""

import argparse
import json
import sys

import keylib


def main():
    parser = argparse.ArgumentParser(description="Resolve/cache the brand-logo-kit API key")
    parser.add_argument("--set", dest="set_key", metavar="KEY", help="Cache this key manually")
    parser.add_argument("--provider", choices=["google", "openrouter", "local"],
                        help="Force provider (also used to disambiguate --set; "
                             "local = on-device image-gen fallback)")
    parser.add_argument("--show", action="store_true", help="Show cached config (key masked)")
    parser.add_argument("--clear", action="store_true", help="Forget the cached key")
    args = parser.parse_args()

    if args.clear:
        keylib.clear_key()
        print(f"Cached key cleared ({keylib.CONFIG_FILE}).")
        return

    if args.show:
        cfg = dict(keylib.load_config())
        if "api_key" in cfg:
            cfg["api_key"] = keylib.mask(cfg.get("api_key", ""))
        cfg.setdefault("config_file", str(keylib.CONFIG_FILE))
        print(json.dumps(cfg, indent=2))
        return

    if args.set_key:
        provider = args.provider or keylib.detect_provider(args.set_key)
        if not provider:
            print("Could not infer provider from the key prefix. "
                  "Re-run with --provider google|openrouter.", file=sys.stderr)
            sys.exit(1)
        keylib.cache_key(args.set_key, provider, source="manual")
        print(f"Saved key: provider={provider} key={keylib.mask(args.set_key)}")
        print(f"  cache: {keylib.CONFIG_FILE}")
        return

    try:
        key, provider, source = keylib.resolve_key(prefer=args.provider)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    if provider == "local":
        print("No API key found — using the on-device LOCAL fallback (image-gen skill).")
    else:
        print("Resolved and cached an API key.")
    print(f"  provider: {provider}")
    print(f"  source:   {source}")
    print(f"  key:      {keylib.mask(key)}")
    print(f"  cache:    {keylib.CONFIG_FILE}")
    if provider != "local":
        print(f"  local fallback available: {keylib.local_available()}")


if __name__ == "__main__":
    main()
