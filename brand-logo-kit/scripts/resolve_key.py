#!/usr/bin/env python3
"""Resolve the provider, or show/set/clear the cached API key for brand-logo-kit.

This repo is local-first: with no arguments the resolver prefers the on-device
image-gen skill (FLUX.2 Klein) when it can realistically run, and only falls
back to a CLOUD key (Gemini/OpenRouter) discovered from the environment or
another installed skill. No key ships with this skill; the cache lives outside
the repo at ~/.brand-logo-kit/config.json.

Usage:
    python3 resolve_key.py                        # resolve provider (local-first), print status
    python3 resolve_key.py --status               # detailed local-provider diagnostics
    python3 resolve_key.py --provider local       # force on-device image-gen
    python3 resolve_key.py --provider google      # force/require a Google key
    python3 resolve_key.py --provider openrouter  # force/require an OpenRouter key
    python3 resolve_key.py --show                 # print cached config (key masked)
    python3 resolve_key.py --set <KEY>            # cache a key manually
    python3 resolve_key.py --clear                # forget the cached key

Env:
    BRAND_LOGO_KIT_PREFER=cloud     try a key before the local model
    BRAND_LOGO_KIT_MIN_DISK_GB=N    free GB required to auto-pick local for a fresh download
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
    parser.add_argument("--status", action="store_true",
                        help="Show detailed local-provider diagnostics (disk, weights, usability)")
    parser.add_argument("--clear", action="store_true", help="Forget the cached key")
    args = parser.parse_args()

    if args.clear:
        keylib.clear_key()
        print(f"Cached key cleared ({keylib.CONFIG_FILE}).")
        return

    if args.status:
        print(json.dumps(keylib.local_status(), indent=2))
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
        st = keylib.local_status()
        if st["weights_present"]:
            why = "weights already downloaded"
        elif st["usable"]:
            why = f"{st['free_gb']} GB free (>= {st['min_free_gb']} GB needed to download)"
        else:
            why = (f"WARNING only {st['free_gb']} GB free, needs ~{st['min_free_gb']} GB "
                   "to download weights — the run may fail")
        print(f"Using the on-device LOCAL model ({st['model']}) — {why}.")
    else:
        st = keylib.local_status()
        reason = "BRAND_LOGO_KIT_PREFER=cloud" if keylib._prefer_cloud() else \
                 ("local not installed" if not st["installed"] else
                  f"local not usable ({st['free_gb']} GB free < {st['min_free_gb']} GB)")
        print(f"Resolved and cached a CLOUD API key (local skipped: {reason}).")
    print(f"  provider: {provider}")
    print(f"  source:   {source}")
    print(f"  key:      {keylib.mask(key)}")
    print(f"  cache:    {keylib.CONFIG_FILE}")


if __name__ == "__main__":
    main()
