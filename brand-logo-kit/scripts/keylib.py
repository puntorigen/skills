#!/usr/bin/env python3
"""Provider resolution + API-key discovery for the brand-logo-kit skill.

This repo is local-first, so the resolver prefers the on-device `image-gen`
skill (FLUX.2 Klein / MLX) whenever it can realistically run — i.e. it is
installed on an Apple Silicon Mac AND either the weights are already downloaded
or there is enough free disk to fetch them. Only when local is not usable does
it fall back to a CLOUD key (Gemini or OpenRouter), which is discovered by
searching, in priority order:

  1. The cached config at ~/.brand-logo-kit/config.json
  2. Environment variables:
        Google:     GEMINI_API_KEY, GOOGLE_API_KEY, GOOGLE_GENAI_API_KEY, GOOGLE_AI_API_KEY
        OpenRouter: OPENROUTER_API_KEY, OPEN_ROUTER_API_KEY
  3. config.json files of other installed skills (e.g. asset-generator)

No key ships with this skill and none is ever written into the repo (the cache
lives outside it). Set BRAND_LOGO_KIT_PREFER=cloud to flip the order (key first),
or force a provider with --provider on the CLI.

Provider is inferred from a key's prefix: "sk-or-" -> openrouter, "AIza" -> google.
"""

import json
import os
import platform
import shutil
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

# HF repo id per local model (to detect already-downloaded weights).
LOCAL_MODEL_REPO = {
    "flux2-klein-4b": "black-forest-labs/FLUX.2-klein-4B",
    "z-image-turbo": "filipstrand/Z-Image-Turbo-mflux-4bit",
}
# Approx free disk (GB, incl. headroom) needed to DOWNLOAD weights the first time.
LOCAL_MIN_FREE_GB = {"flux2-klein-4b": 12.0, "z-image-turbo": 7.0}

GOOGLE_ENV_VARS = ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY", "GOOGLE_AI_API_KEY"]
OPENROUTER_ENV_VARS = ["OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"]

SKILL_ROOTS = [
    Path.home() / ".cursor" / "skills",
    Path.home() / ".claude" / "skills",
    Path.home() / ".config" / "skills",
]

CONFIG_KEY_FIELDS = [
    ("gemini_api_key", "google"),
    ("google_api_key", "google"),
    ("google_genai_api_key", "google"),
    ("openrouter_api_key", "openrouter"),
    ("api_key", None),
]


def detect_provider(key):
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
    """Cache a real (cloud) key. Local is a policy decision and is never cached."""
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
    return "***" if len(key) <= 12 else f"{key[:8]}...{key[-4:]}"


def model_for(provider):
    cfg = load_config()
    if provider == "openrouter":
        return cfg.get("openrouter_model", DEFAULT_OPENROUTER_MODEL)
    if provider == "local":
        return cfg.get("local_model", DEFAULT_LOCAL_MODEL)
    return cfg.get("google_model", DEFAULT_GOOGLE_MODEL)


# ──────────────────────────────────────────────────────────
# Local provider: on-device image-gen skill (mflux / MLX)
# ──────────────────────────────────────────────────────────

def is_apple_silicon():
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def find_image_gen_script():
    candidates = [REPO_ROOT / "image-gen"] + [root / "image-gen" for root in SKILL_ROOTS]
    for d in candidates:
        script = d / "scripts" / "generate_image.py"
        if script.exists():
            return script
    return None


def image_gen_python():
    home = Path(os.environ.get("IMAGE_GEN_HOME", str(Path.home() / ".image-gen")))
    py = home / ".venv" / "bin" / "python"
    return py if py.exists() else None


def hf_hub_dir():
    cache = os.environ.get("HF_HUB_CACHE")
    if cache:
        return Path(cache)
    home = os.environ.get("HF_HOME")
    base = Path(home) if home else (Path.home() / ".cache" / "huggingface")
    return base / "hub"


def _repo_cache_name(repo):
    return "models--" + repo.replace("/", "--")


def local_weights_present(model):
    repo = LOCAL_MODEL_REPO.get(model)
    if not repo:
        return False
    d = hf_hub_dir() / _repo_cache_name(repo)
    try:
        return d.is_dir() and any(d.rglob("*.safetensors"))
    except Exception:
        return False


def free_gb(path):
    try:
        p = Path(path)
        while not p.exists() and p != p.parent:
            p = p.parent
        return shutil.disk_usage(str(p)).free / (1024 ** 3)
    except Exception:
        return 0.0


def min_free_gb(model):
    env = os.environ.get("BRAND_LOGO_KIT_MIN_DISK_GB")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return LOCAL_MIN_FREE_GB.get(model, 12.0)


def enough_disk_for(model):
    return free_gb(hf_hub_dir()) >= min_free_gb(model)


def local_installed():
    """image-gen present + set up on an Apple Silicon Mac (can run at all)."""
    return bool(find_image_gen_script()) and image_gen_python() is not None and is_apple_silicon()


# Backwards-compatible alias.
local_available = local_installed


def local_usable(model=None):
    """local_installed AND (weights already present OR enough disk to download)."""
    if not local_installed():
        return False
    model = model or model_for("local")
    return local_weights_present(model) or enough_disk_for(model)


def local_status(model=None):
    model = model or model_for("local")
    script = find_image_gen_script()
    py = image_gen_python()
    return {
        "model": model,
        "apple_silicon": is_apple_silicon(),
        "image_gen_script": str(script) if script else None,
        "image_gen_python": str(py) if py else None,
        "installed": local_installed(),
        "weights_present": local_weights_present(model),
        "free_gb": round(free_gb(hf_hub_dir()), 1),
        "min_free_gb": min_free_gb(model),
        "usable": local_usable(model),
    }


def _prefer_cloud():
    return os.environ.get("BRAND_LOGO_KIT_PREFER", "").strip().lower() == "cloud"


# ──────────────────────────────────────────────────────────
# Cloud key candidates
# ──────────────────────────────────────────────────────────

def _cached_candidate():
    cfg = load_config()
    key = str(cfg.get("api_key", "")).strip()
    if key:
        provider = cfg.get("provider") or detect_provider(key) or "google"
        return key, provider, cfg.get("key_source") or "cache"
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
    """Yield (key, provider, source) cloud-key tuples in priority order."""
    if include_cache:
        cached = _cached_candidate()
        if cached:
            yield cached
    yield from _env_candidates()
    yield from _sibling_skill_candidates()


def _first_key(prefer=None, write_cache=True):
    for key, provider, source in iter_candidates():
        provider = detect_provider(key) or provider or "google"
        if prefer and provider != prefer:
            continue
        if write_cache and not source.startswith("cache"):
            cache_key(key, provider, source)
        return key, provider, source
    return None


def _local_help():
    if not is_apple_silicon():
        return "  local needs an Apple Silicon Mac.\n"
    if find_image_gen_script() is None:
        return "  install the image-gen skill for a no-key local fallback.\n"
    if image_gen_python() is None:
        return "  run image-gen's setup_env.sh to enable the local fallback.\n"
    st = local_status()
    return (f"  local is installed but not usable: only {st['free_gb']} GB free, "
            f"needs ~{st['min_free_gb']} GB to download weights "
            f"(set BRAND_LOGO_KIT_MIN_DISK_GB to override).\n")


def resolve_key(prefer=None, allow_local=True, write_cache=True):
    """Resolve (key, provider, source). Local returns key="".

    Order (default): local-first if usable, else a cloud key, else local if merely
    installed (last resort). Set BRAND_LOGO_KIT_PREFER=cloud to try a key first.
    """
    if prefer == "local":
        if not local_installed():
            raise RuntimeError("Local provider requested but unavailable.\n" + _local_help())
        return "", "local", "local:image-gen"

    if prefer in ("google", "openrouter"):
        hit = _first_key(prefer=prefer, write_cache=write_cache)
        if hit:
            return hit
        raise RuntimeError(f"No {prefer} API key found.\n"
                           "  Set the matching env var or run resolve_key.py --set <KEY>.")

    # prefer is None: local-first policy (unless BRAND_LOGO_KIT_PREFER=cloud)
    if allow_local and not _prefer_cloud() and local_usable():
        return "", "local", "local:image-gen"

    hit = _first_key(write_cache=write_cache)
    if hit:
        return hit

    if allow_local and local_usable():
        return "", "local", "local:image-gen"
    if allow_local and local_installed():
        return "", "local", "local:image-gen(low-disk)"

    searched = ", ".join(GOOGLE_ENV_VARS + OPENROUTER_ENV_VARS)
    raise RuntimeError(
        "No usable provider found.\n"
        f"  Checked env vars: {searched}\n"
        "  Checked other skills' config.json (e.g. asset-generator).\n"
        + _local_help()
        + "Provide one of:\n"
        "  - set up the image-gen skill (Apple Silicon) for on-device generation\n"
        "  - export GEMINI_API_KEY=AIza...        (Google AI Studio)\n"
        "  - export OPENROUTER_API_KEY=sk-or-...  (OpenRouter)\n"
        "  - python3 scripts/resolve_key.py --set <YOUR_KEY>"
    )


if __name__ == "__main__":
    try:
        k, p, s = resolve_key()
        print(f"provider={p} source={s} key={mask(k)}")
    except RuntimeError as e:
        print(e)
