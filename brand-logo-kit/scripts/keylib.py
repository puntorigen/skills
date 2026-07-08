#!/usr/bin/env python3
"""API-key discovery + provider resolution for the brand-logo-kit skill.

No API key ships with this skill. On first use a usable provider is resolved by
searching, in priority order:

  1. The cached config at ~/.brand-logo-kit/config.json (fast path once resolved)
  2. Environment variables:
        Google:     GEMINI_API_KEY, GOOGLE_API_KEY, GOOGLE_GENAI_API_KEY, GOOGLE_AI_API_KEY
        OpenRouter: OPENROUTER_API_KEY, OPEN_ROUTER_API_KEY
  3. config.json files belonging to other installed skills (e.g. asset-generator)
     under ~/.cursor/skills, ~/.claude/skills, ~/.config/skills
  4. LOCAL FALLBACK: if no key is found, the on-device `image-gen` skill (mflux /
     MLX, Apple Silicon) is used to render the logo locally — no key, no cloud.

The resolved (key, provider) is cached OUTSIDE the repo so later runs are instant
and no secret is ever written into the repository.

Provider is inferred from the key prefix when possible:
    - "sk-or-" -> openrouter
    - "AIza"   -> google (Google AI Studio)
otherwise the source's provider hint is used.
"""

import json
import os
import platform
from pathlib import Path

# State (venv + key cache) lives outside the repo, matching the rest of this repo.
HOME_DIR = Path(os.environ.get("BRAND_LOGO_KIT_HOME", str(Path.home() / ".brand-logo-kit")))
CONFIG_FILE = HOME_DIR / "config.json"

# This skill's source dir (in the repo) + repo root, used to find sibling skills.
SKILL_SRC_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_SRC_DIR.parent

# Model defaults (overridable via config.json or --model on generate.py).
DEFAULT_GOOGLE_MODEL = "gemini-3-pro-image-preview"
DEFAULT_OPENROUTER_MODEL = "google/gemini-3-pro-image"
DEFAULT_LOCAL_MODEL = "flux2-klein-4b"  # FLUX.2 Klein: cleaner for graphic/logo work
LOCAL_MODELS = {"flux2-klein-4b", "z-image-turbo"}

GOOGLE_ENV_VARS = [
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_API_KEY",
    "GOOGLE_AI_API_KEY",
]
OPENROUTER_ENV_VARS = [
    "OPENROUTER_API_KEY",
    "OPEN_ROUTER_API_KEY",
]

# Other skill roots to scan for a reusable key or the image-gen skill.
SKILL_ROOTS = [
    Path.home() / ".cursor" / "skills",
    Path.home() / ".claude" / "skills",
    Path.home() / ".config" / "skills",
]

# Field names inside a sibling config.json that may hold a usable key.
CONFIG_KEY_FIELDS = [
    ("gemini_api_key", "google"),
    ("google_api_key", "google"),
    ("google_genai_api_key", "google"),
    ("openrouter_api_key", "openrouter"),
    ("api_key", None),  # provider inferred from prefix / sibling "provider" field
]


def detect_provider(key):
    """Infer the provider from a key's prefix. Returns 'google', 'openrouter', or None."""
    k = (key or "").strip()
    if k.startswith("sk-or-"):
        return "openrouter"
    if k.startswith("AIza"):
        return "google"
    return None


def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(cfg):
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def cache_key(key, provider, source="manual"):
    cfg = load_config()
    cfg["api_key"] = key.strip()
    cfg["provider"] = provider
    cfg["key_source"] = source
    save_config(cfg)
    return cfg


def clear_key():
    cfg = load_config()
    for field in ("api_key", "provider", "key_source"):
        cfg.pop(field, None)
    save_config(cfg)
    return cfg


def mask(key):
    if not key:
        return "(none — local, no key)"
    key = key.strip()
    if len(key) <= 12:
        return "***"
    return f"{key[:8]}...{key[-4:]}"


def model_for(provider):
    cfg = load_config()
    if provider == "openrouter":
        return cfg.get("openrouter_model", DEFAULT_OPENROUTER_MODEL)
    if provider == "local":
        return cfg.get("local_model", DEFAULT_LOCAL_MODEL)
    return cfg.get("google_model", DEFAULT_GOOGLE_MODEL)


# ──────────────────────────────────────────────────────────
# Local fallback: the on-device image-gen skill (mflux / MLX)
# ──────────────────────────────────────────────────────────

def is_apple_silicon():
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def find_image_gen_script():
    """Locate image-gen/scripts/generate_image.py (sibling in the repo, or installed)."""
    candidates = [REPO_ROOT / "image-gen"]
    candidates += [root / "image-gen" for root in SKILL_ROOTS]
    for d in candidates:
        script = d / "scripts" / "generate_image.py"
        if script.exists():
            return script
    return None


def image_gen_python():
    """Return the image-gen venv python if its setup has been run, else None."""
    home = Path(os.environ.get("IMAGE_GEN_HOME", str(Path.home() / ".image-gen")))
    py = home / ".venv" / "bin" / "python"
    return py if py.exists() else None


def local_available():
    """True when local fallback can realistically run (script + venv + Apple Silicon)."""
    return bool(find_image_gen_script()) and image_gen_python() is not None and is_apple_silicon()


def _cached_candidate():
    cfg = load_config()
    key = str(cfg.get("api_key", "")).strip()
    provider = cfg.get("provider")
    if key:
        prov = provider or detect_provider(key) or "google"
        return key, prov, cfg.get("key_source") or "cache"
    if provider == "local":
        return "", "local", cfg.get("key_source") or "cache:local"
    return None


def _env_candidates():
    for var in GOOGLE_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            yield val, "google", f"env:{var}"
    for var in OPENROUTER_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            yield val, "openrouter", f"env:{var}"


def _sibling_skill_candidates():
    for root in SKILL_ROOTS:
        if not root.is_dir():
            continue
        for cfg_path in sorted(root.glob("*/config.json")):
            try:
                if cfg_path.resolve() == CONFIG_FILE.resolve():
                    continue
            except Exception:
                pass
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            sibling_provider = str(data.get("provider", "")).strip().lower() or None
            for field, hint in CONFIG_KEY_FIELDS:
                val = str(data.get(field, "")).strip()
                if not val:
                    continue
                provider = detect_provider(val) or hint or sibling_provider or "google"
                yield val, provider, f"skill:{cfg_path.parent.name}:{field}"


def iter_candidates(include_cache=True):
    """Yield (key, provider, source) tuples in resolution priority order (keys only)."""
    if include_cache:
        cached = _cached_candidate()
        if cached and cached[1] != "local":
            yield cached
    yield from _env_candidates()
    yield from _sibling_skill_candidates()


def resolve_key(prefer=None, allow_local=True, write_cache=True):
    """Resolve a usable (key, provider, source).

    prefer: optionally restrict to 'google', 'openrouter', or 'local'.
    allow_local: fall back to on-device image-gen when no key is found.
    Returns key="" for the local provider.
    Raises RuntimeError with guidance if nothing usable is found.
    """
    if prefer == "local":
        if find_image_gen_script() is None:
            raise RuntimeError(
                "Local fallback requested but the 'image-gen' skill was not found.\n"
                "Install it next to this skill (e.g. npx skills add puntorigen/skills@image-gen)."
            )
        if image_gen_python() is None:
            raise RuntimeError(
                "The 'image-gen' skill is present but not set up.\n"
                "Run its setup once: bash <image-gen>/scripts/setup_env.sh"
            )
        if write_cache:
            cache_key("", "local", "local:image-gen")
        return "", "local", "local:image-gen"

    for key, provider, source in iter_candidates():
        provider = detect_provider(key) or provider or "google"
        if prefer and provider != prefer:
            continue
        if write_cache and not source.startswith("cache"):
            cache_key(key, provider, source)
        return key, provider, source

    if allow_local and prefer is None and local_available():
        if write_cache:
            cache_key("", "local", "local:image-gen")
        return "", "local", "local:image-gen"

    searched = ", ".join(GOOGLE_ENV_VARS + OPENROUTER_ENV_VARS)
    local_hint = ""
    if not is_apple_silicon():
        local_hint = "  (local fallback needs an Apple Silicon Mac + the image-gen skill)\n"
    elif find_image_gen_script() is None:
        local_hint = "  (or install the local image-gen skill for a no-key fallback)\n"
    elif image_gen_python() is None:
        local_hint = "  (or run image-gen's setup_env.sh to enable the local no-key fallback)\n"
    raise RuntimeError(
        "No Gemini or OpenRouter API key found, and no local fallback available.\n"
        f"  Checked env vars: {searched}\n"
        "  Checked other skills' config.json (e.g. asset-generator).\n"
        + local_hint
        + "Provide one of:\n"
        "  - export GEMINI_API_KEY=AIza...        (Google AI Studio)\n"
        "  - export OPENROUTER_API_KEY=sk-or-...  (OpenRouter)\n"
        "  - python3 scripts/resolve_key.py --set <YOUR_KEY>\n"
        "  - set up the image-gen skill for an on-device fallback"
    )


if __name__ == "__main__":
    try:
        k, p, s = resolve_key()
        print(f"provider={p} source={s} key={mask(k)}")
    except RuntimeError as e:
        print(e)
