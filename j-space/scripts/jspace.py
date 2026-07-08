#!/usr/bin/env python3
"""j-space: an external, persistent "mental workspace" for a coding agent.

Inspired by Anthropic's global-workspace research (the "J-space"): a small,
inspectable set of word-linked concepts an agent reasons with. This CLI builds a
weighted concept graph, compiles it into a matrix with local embeddings, and lets
the agent load / query / edit / checkpoint / hypnotize that workspace so useful
vocabulary stays "lit up" and consistent across sessions.

Run this with the j-space venv python (created by scripts/setup_env.sh):
  ~/.j-space/.venv/bin/python jspace.py build my-topic

All workspace DATA lives under ./.jspace/ in the current working directory (the
project you are analyzing) - never in the venv/home and never in the skill repo.

Subcommands:
  scan <dir> --name N     draft a graph.json from a codebase
  build <name>            compile graph.json -> matrix.npz + digest.md
  load [name]             print the workspace digest + currently-active concepts
  query "text"            spreading activation over the workspace
  edit ...                mutate the graph (add/link/boost/lower/remove) + rebuild
  checkpoint ...          save / list / restore full-state snapshots
  hypnotize ...           install pins / triggers / suppressions (+ induction)
  wake                    lift the induction and restore pre-hypnosis activation

Convention: progress + diagnostics go to stderr; machine-consumable results go to
stdout. Exit codes: 0 ok, 1 usage/data error, 2 workspace not found.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384

# Digest / clustering tunables (see REFERENCE.md).
CLUSTER_THRESHOLD = 0.15
TOP_CONCEPTS = 20
SEED_K = 8            # max nodes seeded by a query
SEED_REL = 0.85       # a node seeds only if cosine >= SEED_REL * best match
SEED_FLOOR = 0.2      # ...and above this absolute cosine floor
TRIGGER_FIRE = 0.2    # activation of `when` needed to fire a trigger
TRIGGER_LIGHT = 0.8   # activation forced onto triggered concepts
SUPPRESS_FACTOR = 0.2 # multiplier applied to suppressed concepts (soft)

SCAN_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "dist", "build",
    "__pycache__", ".jspace", ".idea", ".vscode", ".next", "target",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "vendor",
}
SCAN_MAX_BYTES = 1_000_000

STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "her",
    "was", "one", "our", "out", "day", "get", "has", "him", "his", "how", "man",
    "new", "now", "old", "see", "two", "way", "who", "boy", "did", "its", "let",
    "put", "say", "she", "too", "use", "this", "that", "with", "from", "have",
    "will", "your", "they", "would", "there", "their", "what", "about", "which",
    "when", "make", "like", "time", "just", "know", "take", "into", "them",
    "then", "than", "some", "could", "other", "these", "also", "been", "were",
    "such", "only", "here", "each", "more", "most", "over", "used", "using",
    "self", "none", "true", "false", "return", "returns", "value", "values",
    "type", "types", "list", "dict", "str", "int", "obj", "args", "kwargs",
    "def", "var", "let", "const", "function", "class", "import", "export",
    "print", "test", "tests", "data", "name", "names", "file", "files", "path",
    "paths", "text", "line", "lines", "code", "main", "run", "set", "add",
    "get", "new", "end", "start", "init", "config", "result", "results",
}
# Reserved words across common languages we do not want as concepts.
KEYWORDS = {
    "public", "private", "protected", "static", "void", "final", "abstract",
    "async", "await", "yield", "throw", "throws", "catch", "try", "finally",
    "while", "elif", "else", "case", "switch", "break", "continue", "default",
    "package", "module", "namespace", "interface", "extends", "implements",
    "struct", "enum", "trait", "impl", "match", "where", "super", "lambda",
    "global", "nonlocal", "assert", "del", "pass", "raise", "with", "boolean",
    "string", "number", "float", "double", "char", "byte", "long", "short",
    "unsigned", "signed", "typedef", "template", "typename", "virtual",
    "override", "readonly", "extern", "inline", "goto", "sizeof",
}


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #

def eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)
    sys.stderr.flush()


def die(msg: str) -> "None":
    """Data / usage error -> exit 1."""
    eprint(f"[jspace] error: {msg}")
    raise SystemExit(1)


def not_found(msg: str) -> "None":
    """Workspace-not-found -> exit 2."""
    eprint(f"[jspace] {msg}")
    raise SystemExit(2)


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or "unlabeled"


def jspace_home() -> str:
    return os.environ.get("JSPACE_HOME", os.path.expanduser("~/.j-space"))


# --------------------------------------------------------------------------- #
# project-local paths (everything under ./.jspace/)
# --------------------------------------------------------------------------- #

def js_dir() -> Path:
    return Path(".jspace")


def state_path() -> Path:
    return js_dir() / "state.json"


def ws_dir(name: str) -> Path:
    return js_dir() / name


def graph_path(name: str) -> Path:
    return ws_dir(name) / "graph.json"


def matrix_path(name: str) -> Path:
    return ws_dir(name) / "matrix.npz"


def digest_path(name: str) -> Path:
    return ws_dir(name) / "digest.md"


def induction_path(name: str) -> Path:
    return ws_dir(name) / "induction.md"


def ckpt_dir(name: str) -> Path:
    return ws_dir(name) / "checkpoints"


def rule_path(name: str) -> Path:
    return Path(".cursor") / "rules" / f"jspace-{name}.mdc"


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

def default_state() -> dict:
    return {"current": None, "workspaces": {}}


def load_state() -> dict:
    p = state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError as e:
            die(f"corrupt state file {p}: {e}")
    return default_state()


def save_state(state: dict) -> None:
    atomic_write_json(state_path(), state)


def default_ws_entry() -> dict:
    return {
        "activation": [],
        "pins": {},
        "triggers": [],
        "suppress": [],
        "last_query": None,
        "alpha": 0.75,
        "sim_threshold": 0.35,
        "updated": now_iso(),
    }


def resolve_name(state: dict, name: str | None) -> str:
    if name:
        return name
    cur = state.get("current")
    if not cur:
        not_found("no workspace given and no current workspace set - run 'build <name>' first.")
    return cur


def require_workspace(state: dict, name: str) -> dict:
    ws = state.get("workspaces", {}).get(name)
    if ws is None or not matrix_path(name).exists():
        avail = ", ".join(state.get("workspaces", {}).keys()) or "(none)"
        not_found(f"unknown workspace {name!r}. Available: {avail}")
    return ws


# --------------------------------------------------------------------------- #
# embeddings (local, offline after setup)
# --------------------------------------------------------------------------- #

_MODEL = None


def get_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        from sentence_transformers import SentenceTransformer
    except Exception as e:  # noqa: BLE001
        die(f"sentence-transformers not available ({e}). Run scripts/setup_env.sh "
            "and use the j-space venv python.")
    device = None
    try:
        import torch
        if torch.backends.mps.is_available():
            device = "mps"
        elif torch.cuda.is_available():
            device = "cuda"
    except Exception:  # noqa: BLE001
        device = None
    cache = os.path.join(jspace_home(), "models")
    eprint(f"[jspace] loading embedding model ({MODEL_ID}, device={device or 'cpu'})...")
    _MODEL = SentenceTransformer(MODEL_ID, cache_folder=cache, device=device)
    return _MODEL


def embed_texts(texts: list[str]) -> np.ndarray:
    """Return an (len(texts), 384) float32 array of unit-normalized embeddings."""
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)
    model = get_model()
    vecs = model.encode(
        texts, normalize_embeddings=True, show_progress_bar=False,
        convert_to_numpy=True,
    )
    return np.asarray(vecs, dtype=np.float32)


def node_text(node: dict) -> str:
    """The string we embed for a node: 'word — note' when a note exists."""
    note = node.get("note")
    return f"{node['word']} — {note}" if note else node["word"]


# --------------------------------------------------------------------------- #
# graph validation
# --------------------------------------------------------------------------- #

def read_graph(name: str) -> dict:
    p = graph_path(name)
    if not p.exists():
        not_found(f"no graph at {p}. Author it (topic research) or run 'scan' first.")
    try:
        graph = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {p}: {e}")
    validate_graph(graph)
    return graph


def validate_graph(graph: dict) -> None:
    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        die("graph has no nodes.")
    seen = set()
    for n in nodes:
        w = n.get("word")
        if not w or not isinstance(w, str):
            die(f"node missing a string 'word': {n!r}")
        if w in seen:
            die(f"duplicate node word: {w!r}")
        seen.add(w)
        if "weight" not in n:
            die(f"node {w!r} missing 'weight'")
    for e in graph.get("edges", []) or []:
        for side in ("a", "b"):
            if e.get(side) not in seen:
                die(f"edge references unknown word: {e.get(side)!r}")


# --------------------------------------------------------------------------- #
# matrix construction
# --------------------------------------------------------------------------- #

def build_arrays(graph: dict, sim_threshold: float,
                 embed_cache: dict[str, np.ndarray] | None = None):
    """Return (vocab, W, E, a, n_explicit, n_sim) from a validated graph.

    embed_cache maps node_text -> vector; hits are reused (incremental rebuild),
    misses are batch-embedded.
    """
    nodes = graph["nodes"]
    vocab = [n["word"] for n in nodes]
    n = len(vocab)
    idx = {w: i for i, w in enumerate(vocab)}

    texts = [node_text(nd) for nd in nodes]
    cache = embed_cache or {}
    missing = [t for t in texts if t not in cache]
    if missing:
        uniq = list(dict.fromkeys(missing))
        vecs = embed_texts(uniq)
        for t, v in zip(uniq, vecs):
            cache[t] = v
    E = np.zeros((n, EMBED_DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        E[i] = cache[t]

    # explicit edges first (they win over similarity edges)
    W = np.zeros((n, n), dtype=np.float32)
    explicit_pairs: set[tuple[int, int]] = set()
    for e in graph.get("edges", []) or []:
        i, j = idx[e["a"]], idx[e["b"]]
        if i == j:
            continue
        w = float(e.get("w", 0.5))
        W[i, j] = w
        W[j, i] = w
        explicit_pairs.add((min(i, j), max(i, j)))

    # similarity edges for pairs above threshold with no explicit edge
    n_sim = 0
    if n > 1:
        sim = E @ E.T  # cosine (unit-norm embeddings)
        for i in range(n):
            for j in range(i + 1, n):
                if (i, j) in explicit_pairs:
                    continue
                c = float(sim[i, j])
                if c >= sim_threshold:
                    W[i, j] = c
                    W[j, i] = c
                    n_sim += 1

    np.fill_diagonal(W, 0.0)

    # row-normalize (each row sums to 1; zero rows stay zero)
    rowsum = W.sum(axis=1, keepdims=True)
    Wn = np.divide(W, rowsum, out=np.zeros_like(W), where=rowsum > 0).astype(np.float32)

    # baseline activation from node weights, normalized to max 1.0
    weights = np.array([float(nd["weight"]) for nd in nodes], dtype=np.float32)
    mx = float(weights.max()) if n else 0.0
    a = (weights / mx).astype(np.float32) if mx > 0 else weights
    return vocab, Wn, E, a, len(explicit_pairs), n_sim


def save_matrix(name: str, vocab, W, E, a) -> None:
    ws_dir(name).mkdir(parents=True, exist_ok=True)
    np.savez(
        matrix_path(name),
        vocab=np.array(list(vocab)),
        W=W.astype(np.float32),
        E=E.astype(np.float32),
        a=a.astype(np.float32),
    )


def load_matrix(name: str):
    p = matrix_path(name)
    if not p.exists():
        not_found(f"no compiled matrix at {p} - run 'build {name}' first.")
    data = np.load(p, allow_pickle=False)
    vocab = [str(x) for x in data["vocab"].tolist()]
    return vocab, data["W"], data["E"], data["a"]


# --------------------------------------------------------------------------- #
# graph analysis for the digest
# --------------------------------------------------------------------------- #

def strongest_neighbors(W: np.ndarray, i: int, k: int) -> list[int]:
    scores = np.maximum(W[i], W[:, i]).copy()
    scores[i] = -1.0
    order = np.argsort(-scores)
    return [int(j) for j in order[:k] if scores[int(j)] > 0]


def connected_components(W: np.ndarray, threshold: float) -> list[list[int]]:
    n = W.shape[0]
    adj = (W >= threshold) | (W.T >= threshold)
    seen = [False] * n
    comps = []
    for start in range(n):
        if seen[start]:
            continue
        stack = [start]
        seen[start] = True
        comp = []
        while stack:
            u = stack.pop()
            comp.append(u)
            row = adj[u]
            for v in range(n):
                if row[v] and not seen[v]:
                    seen[v] = True
                    stack.append(v)
        comps.append(comp)
    comps.sort(key=len, reverse=True)
    return comps


def thought_sequence(vocab: list[str], W: np.ndarray, a: np.ndarray) -> list[str]:
    """Greedy activation-weighted traversal: start at the highest-a node, hop to
    the strongest unvisited neighbor, restart at the next-highest-a node when
    stuck. Covers every node."""
    n = len(vocab)
    visited = [False] * n
    order = sorted(range(n), key=lambda i: float(a[i]), reverse=True)
    seq: list[int] = []
    for seed in order:
        if visited[seed]:
            continue
        cur = seed
        visited[cur] = True
        seq.append(cur)
        while True:
            best_j, best_w = -1, 0.0
            col = W[:, cur]
            row = W[cur]
            for j in range(n):
                if visited[j]:
                    continue
                wj = max(float(row[j]), float(col[j]))
                if wj > best_w:
                    best_w, best_j = wj, j
            if best_j < 0 or best_w <= 0.0:
                break
            visited[best_j] = True
            seq.append(best_j)
            cur = best_j
    return [vocab[i] for i in seq]


def render_digest(name: str, graph: dict, vocab, W, a) -> str:
    nodes = graph["nodes"]
    idx = {w: i for i, w in enumerate(vocab)}
    weight_of = {n["word"]: float(n["weight"]) for n in nodes}
    note_of = {n["word"]: n.get("note") for n in nodes}

    out = [f"# J-space: {name}", ""]
    topic = graph.get("topic", "")
    if topic:
        out += [f"_{topic}_", ""]

    out += ["## Top concepts", ""]
    top = sorted(vocab, key=lambda w: -weight_of.get(w, 0.0))[:TOP_CONCEPTS]
    for w in top:
        note = note_of.get(w)
        line = f"- **{w}** ({weight_of.get(w, 0.0):.2f})"
        if note:
            line += f" — {note}"
        out.append(line)
    out.append("")

    out += ["## Clusters", ""]
    comps = [c for c in connected_components(np.asarray(W), CLUSTER_THRESHOLD) if len(c) >= 2][:8]
    if comps:
        for c in comps:
            words = sorted((vocab[i] for i in c), key=lambda w: -weight_of.get(w, 0.0))
            out.append(f"- {', '.join(words)}")
    else:
        out.append("_(no strong clusters at threshold "
                   f"{CLUSTER_THRESHOLD}; concepts are loosely connected)_")
    out.append("")

    out += ["## Thought sequence", ""]
    seq = thought_sequence(vocab, np.asarray(W), np.asarray(a))
    out.append(" → ".join(seq))
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# compile (shared by build / edit / restore)
# --------------------------------------------------------------------------- #

def filter_hypnosis_to_vocab(entry: dict, vocab: list[str]) -> None:
    """Drop pins/triggers/suppressions that reference words no longer present."""
    vset = set(vocab)
    entry["pins"] = {w: s for w, s in (entry.get("pins") or {}).items() if w in vset}
    entry["suppress"] = [w for w in (entry.get("suppress") or []) if w in vset]
    new_triggers = []
    for rule in entry.get("triggers") or []:
        if rule.get("when") not in vset:
            continue
        lights = [w for w in rule.get("light", []) if w in vset]
        if lights:
            new_triggers.append({"when": rule["when"], "light": lights})
    entry["triggers"] = new_triggers


def compile_workspace(name: str, graph: dict, sim_threshold: float, alpha: float,
                      embed_cache: dict[str, np.ndarray] | None = None) -> tuple[int, int, int]:
    vocab, W, E, a, n_explicit, n_sim = build_arrays(graph, sim_threshold, embed_cache)
    save_matrix(name, vocab, W, E, a)
    atomic_write_text(digest_path(name), render_digest(name, graph, vocab, W, a))

    state = load_state()
    entry = state.setdefault("workspaces", {}).get(name) or default_ws_entry()
    entry["activation"] = [float(x) for x in a]  # reset live activation to baseline
    entry["alpha"] = float(alpha)
    entry["sim_threshold"] = float(sim_threshold)
    entry.setdefault("pins", {})
    entry.setdefault("triggers", [])
    entry.setdefault("suppress", [])
    entry.setdefault("last_query", None)
    filter_hypnosis_to_vocab(entry, vocab)
    entry["updated"] = now_iso()
    state["workspaces"][name] = entry
    state["current"] = name
    save_state(state)
    return len(vocab), n_explicit, n_sim


# --------------------------------------------------------------------------- #
# subcommand: build
# --------------------------------------------------------------------------- #

def cmd_build(args) -> int:
    name = args.name
    graph = read_graph(name)
    if graph.get("name") and graph["name"] != name:
        eprint(f"[jspace] note: graph 'name' is {graph['name']!r} but building as {name!r}.")
    n, n_explicit, n_sim = compile_workspace(name, graph, args.sim_threshold, args.alpha)
    eprint(f"[jspace] built '{name}': {n} nodes, "
           f"{n_explicit} explicit + {n_sim} similarity edges (threshold {args.sim_threshold}).")
    print(f"workspace: {name}")
    print(f"nodes: {n}")
    print(f"edges: {n_explicit} explicit, {n_sim} similarity")
    print(f"digest: {digest_path(name)}")
    return 0


# --------------------------------------------------------------------------- #
# subcommand: load
# --------------------------------------------------------------------------- #

def cmd_load(args) -> int:
    state = load_state()
    name = resolve_name(state, args.name)
    entry = require_workspace(state, name)
    state["current"] = name
    save_state(state)

    dp = digest_path(name)
    if dp.exists():
        print(dp.read_text().rstrip())
    print()
    print("## Currently active")
    print()
    vocab, W, E, _a = load_matrix(name)
    a = np.array(entry.get("activation") or [], dtype=np.float32)
    if a.size != len(vocab):
        _v, _W, _E, base = load_matrix(name)
        a = np.array(base, dtype=np.float32)
    pins = entry.get("pins", {})
    order = np.argsort(-a)[:15]
    for i in order:
        i = int(i)
        nbrs = [vocab[j] for j in strongest_neighbors(np.asarray(W), i, 3)]
        tag = " [pinned]" if vocab[i] in pins else ""
        arrow = f" → {', '.join(nbrs)}" if nbrs else ""
        print(f"- **{vocab[i]}** ({float(a[i]):.2f}){tag}{arrow}")
    print()
    eprint(f"[jspace] loaded '{name}' as the current workspace.")
    return 0


# --------------------------------------------------------------------------- #
# subcommand: query
# --------------------------------------------------------------------------- #

def cmd_query(args) -> int:
    state = load_state()
    name = resolve_name(state, args.name)
    entry = require_workspace(state, name)
    text = args.text
    if not text or not text.strip():
        die("query text is empty.")

    vocab, W, E, base = load_matrix(name)
    W = np.asarray(W, dtype=np.float32)
    E = np.asarray(E, dtype=np.float32)
    n = len(vocab)
    idx = {w: i for i, w in enumerate(vocab)}
    alpha = float(entry.get("alpha", 0.75))

    q = embed_texts([text])[0]
    sims = E @ q
    order = [int(i) for i in np.argsort(-sims)]
    best = float(sims[order[0]]) if order else 0.0
    cutoff = max(SEED_FLOOR, SEED_REL * best)
    # Seed only the nodes the query is genuinely "about": the strongest match plus
    # anything within a relevance band of it (capped at SEED_K). Everything else
    # that lights up does so via spreading activation and is tagged [assoc].
    nearest = [i for i in order[:SEED_K] if float(sims[i]) >= cutoff]
    if not nearest and order:
        nearest = [order[0]]
    seed_set = set(nearest)
    s = np.zeros(n, dtype=np.float32)
    for i in nearest:
        s[i] = max(0.0, float(sims[i]))

    a = np.array(entry.get("activation") or base, dtype=np.float32)
    if a.size != n:
        a = np.array(base, dtype=np.float32)
    for _ in range(max(1, args.iterations)):
        a = alpha * (W @ a) + (1.0 - alpha) * s
        mx = float(a.max())
        if mx > 0:
            a = a / mx

    # hypnosis, in order: suppress -> pins -> triggers
    pins = entry.get("pins", {})
    suppress = entry.get("suppress", [])
    triggers = entry.get("triggers", [])
    for w in suppress:
        if w in idx:
            a[idx[w]] *= SUPPRESS_FACTOR
    for w, strength in pins.items():
        if w in idx:
            a[idx[w]] = max(float(a[idx[w]]), float(strength))
    triggered: set[int] = set()
    for rule in triggers:
        when = rule.get("when")
        if when in idx and float(a[idx[when]]) >= TRIGGER_FIRE:
            for light in rule.get("light", []):
                if light in idx:
                    a[idx[light]] = max(float(a[idx[light]]), TRIGGER_LIGHT)
                    triggered.add(idx[light])

    def provenance(i: int) -> str:
        w = vocab[i]
        if w in pins:
            return "[pin]"
        if i in triggered:
            return "[trigger]"
        if i in seed_set:
            return "[seed]"
        return "[assoc]"

    order = [int(i) for i in np.argsort(-a)][: args.top]
    print(f"# query: {text}")
    print(f"_workspace: {name}_")
    print()
    print("## Lit up")
    print()
    for i in order:
        if float(a[i]) <= 0:
            continue
        print(f"- **{vocab[i]}** ({float(a[i]):.2f}) {provenance(i)}")

    low = text.lower()
    unmentioned = [vocab[i] for i in order
                   if vocab[i].lower() not in low and i not in seed_set][:5]
    if unmentioned:
        print()
        print("## Related but unmentioned")
        print()
        print(", ".join(unmentioned))
    print()

    entry["activation"] = [float(x) for x in a]
    entry["last_query"] = text
    entry["updated"] = now_iso()
    state["workspaces"][name] = entry
    save_state(state)
    eprint(f"[jspace] query updated the live activation of '{name}'.")
    return 0


# --------------------------------------------------------------------------- #
# subcommand: edit
# --------------------------------------------------------------------------- #

def _parse_add(spec: str) -> dict:
    # word:weight[:note]
    parts = spec.split(":", 2)
    if len(parts) < 2:
        die(f"--add expects word:weight[:note], got {spec!r}")
    word = parts[0].strip()
    try:
        weight = float(parts[1])
    except ValueError:
        die(f"--add weight must be a number, got {parts[1]!r}")
    node = {"word": word, "weight": max(0.0, min(1.0, weight))}
    if len(parts) == 3 and parts[2].strip():
        node["note"] = parts[2].strip()
    return node


def _parse_link(spec: str) -> dict:
    parts = spec.split(":")
    if len(parts) != 3:
        die(f"--link expects a:b:w, got {spec!r}")
    try:
        w = float(parts[2])
    except ValueError:
        die(f"--link weight must be a number, got {parts[2]!r}")
    return {"a": parts[0].strip(), "b": parts[1].strip(), "w": max(0.0, min(1.0, w))}


def cmd_edit(args) -> int:
    state = load_state()
    name = resolve_name(state, args.name)
    require_workspace(state, name)

    old_graph = read_graph(name)
    nodes = old_graph["nodes"]
    edges = old_graph.setdefault("edges", [])
    by_word = {n["word"]: n for n in nodes}
    messages: list[str] = []

    for spec in args.add or []:
        node = _parse_add(spec)
        if node["word"] in by_word:
            die(f"--add: node already exists: {node['word']!r}")
        nodes.append(node)
        by_word[node["word"]] = node
        messages.append(f"added {node['word']!r} (weight {node['weight']:.2f})")

    for spec in args.link or []:
        e = _parse_link(spec)
        if e["a"] not in by_word:
            die(f"--link: unknown word: {e['a']!r}")
        if e["b"] not in by_word:
            die(f"--link: unknown word: {e['b']!r}")
        replaced = False
        for existing in edges:
            if {existing["a"], existing["b"]} == {e["a"], e["b"]}:
                existing["w"] = e["w"]
                replaced = True
                break
        if not replaced:
            edges.append(e)
        messages.append(f"linked {e['a']!r}–{e['b']!r} (w {e['w']:.2f})")

    for word in args.boost or []:
        if word not in by_word:
            die(f"--boost: missing word: {word!r}")
        w = min(1.0, float(by_word[word]["weight"]) * 1.5)
        by_word[word]["weight"] = w
        messages.append(f"boosted {word!r} -> {w:.2f}")

    for word in args.lower or []:
        if word not in by_word:
            die(f"--lower: missing word: {word!r}")
        w = float(by_word[word]["weight"]) * 0.5
        by_word[word]["weight"] = w
        messages.append(f"lowered {word!r} -> {w:.2f}")

    for word in args.remove or []:
        if word not in by_word:
            die(f"--remove: missing word: {word!r}")
        old_graph["nodes"] = [n for n in nodes if n["word"] != word]
        nodes = old_graph["nodes"]
        old_graph["edges"] = [e for e in edges if word not in (e["a"], e["b"])]
        edges = old_graph["edges"]
        by_word.pop(word, None)
        messages.append(f"removed {word!r} (and its edges/pins/triggers)")

    if not messages:
        die("no edit operations given (use --add/--link/--boost/--lower/--remove).")

    validate_graph(old_graph)

    # incremental embedding cache: reuse rows whose node_text is unchanged
    cache: dict[str, np.ndarray] = {}
    if matrix_path(name).exists():
        try:
            old_vocab, _W, oldE, _a = load_matrix(name)
            prev_nodes = {n["word"]: n for n in json.loads(graph_path(name).read_text()).get("nodes", [])}
            for i, w in enumerate(old_vocab):
                if w in prev_nodes:
                    cache[node_text(prev_nodes[w])] = oldE[i]
        except Exception:  # noqa: BLE001
            cache = {}

    atomic_write_json(graph_path(name), old_graph)
    alpha = float(state["workspaces"][name].get("alpha", 0.75))
    sim_threshold = float(state["workspaces"][name].get("sim_threshold", 0.35))
    n, n_explicit, n_sim = compile_workspace(name, old_graph, sim_threshold, alpha, embed_cache=cache)

    for m in messages:
        eprint(f"[jspace]   {m}")
    eprint(f"[jspace] rebuilt '{name}': {n} nodes, {n_explicit} explicit + {n_sim} similarity edges.")
    for m in messages:
        print(m)
    return 0


# --------------------------------------------------------------------------- #
# subcommand: scan
# --------------------------------------------------------------------------- #

_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]{2,}|[A-Z]{2,}")


def _split_identifier(tok: str) -> list[str]:
    out: list[str] = []
    for piece in tok.split("_"):
        out += _CAMEL_RE.findall(piece)
    return out


def _read_text_file(path: Path) -> str | None:
    try:
        if path.stat().st_size > SCAN_MAX_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _walk_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SCAN_SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            yield Path(dirpath) / fn


def _extract_terms(text: str) -> list[str]:
    terms: list[str] = []
    for tok in _IDENT_RE.findall(text):
        for w in _split_identifier(tok):
            w = w.lower()
            if len(w) < 3 or w in STOPWORDS or w in KEYWORDS:
                continue
            if not w.isalpha():
                continue
            terms.append(w)
    return terms


def cmd_scan(args) -> int:
    root = Path(args.dir)
    if not root.is_dir():
        die(f"not a directory: {root}")
    name = args.name
    gp = graph_path(name)
    if gp.exists() and not args.force:
        die(f"graph already exists at {gp} (use --force to overwrite).")

    tf: Counter[str] = Counter()
    df: Counter[str] = Counter()
    file_term_sets: list[set[str]] = []
    n_files = 0
    for path in _walk_files(root):
        text = _read_text_file(path)
        if text is None:
            continue
        n_files += 1
        terms = _extract_terms(text)
        if not terms:
            continue
        tset = set(terms)
        tf.update(terms)
        df.update(tset)
        file_term_sets.append(tset)

    if not tf:
        die(f"no usable terms found under {root} (empty or all-binary?).")

    scores: dict[str, float] = {}
    for term, freq in tf.items():
        scores[term] = freq * math.log(1.0 + n_files / df[term])
    top_terms = [t for t, _ in sorted(scores.items(), key=lambda kv: -kv[1])[: args.top]]
    top_set = set(top_terms)

    vals = [scores[t] for t in top_terms]
    lo, hi = min(vals), max(vals)

    def norm_weight(v: float) -> float:
        return round(0.2 + 0.8 * (v - lo) / (hi - lo), 3) if hi > lo else 1.0

    nodes = [{"word": t, "weight": norm_weight(scores[t])} for t in top_terms]

    cooc: Counter[tuple[str, str]] = Counter()
    for tset in file_term_sets:
        present = sorted(tset & top_set)
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                cooc[(present[i], present[j])] += 1
    max_c = max(cooc.values()) if cooc else 1

    per_node: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for (a, b), c in cooc.items():
        w = min(1.0, c / max_c)
        per_node[a].append((w, b))
        per_node[b].append((w, a))
    kept: set[tuple[str, str]] = set()
    edges = []
    for node, lst in per_node.items():
        lst.sort(reverse=True)
        for w, other in lst[:6]:
            key = tuple(sorted((node, other)))
            if key in kept:
                continue
            kept.add(key)
            edges.append({"a": key[0], "b": key[1], "w": round(w, 3)})

    graph = {
        "name": name,
        "topic": f"codebase: {args.dir}",
        "created": now_iso(),
        "nodes": nodes,
        "edges": edges,
    }
    atomic_write_json(gp, graph)

    eprint(f"[jspace] scanned {n_files} files under {root}; "
           f"kept {len(nodes)} terms, {len(edges)} edges. Draft graph: {gp}")
    eprint("[jspace] curate the graph (rename/merge concepts, fix weights), then 'build'.")
    print(f"graph: {gp}")
    print(f"{'term':<28} {'weight':>7}  files")
    for t in top_terms:
        print(f"{t:<28} {norm_weight(scores[t]):>7.3f}  {df[t]}")
    return 0


# --------------------------------------------------------------------------- #
# subcommand: checkpoint
# --------------------------------------------------------------------------- #

def _save_checkpoint(name: str, label: str, state: dict) -> Path:
    vocab, W, E, _a = load_matrix(name)
    entry = state["workspaces"][name]
    a = np.array(entry.get("activation") or _a, dtype=np.float32)
    if a.size != len(vocab):
        a = np.asarray(_a, dtype=np.float32)
    graph = json.loads(graph_path(name).read_text()) if graph_path(name).exists() else {}
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"{ts}-{slugify(label)}.npz"
    dest = ckpt_dir(name) / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        dest,
        vocab=np.array(list(vocab)),
        W=np.asarray(W, dtype=np.float32),
        E=np.asarray(E, dtype=np.float32),
        a=a,
        state_json=np.array(json.dumps(entry)),
        graph_json=np.array(json.dumps(graph)),
    )
    return dest


def _parse_ckpt_name(fname: str) -> tuple[str, str]:
    stem = fname[:-4] if fname.endswith(".npz") else fname
    parts = stem.split("-")
    if len(parts) >= 3:
        return f"{parts[0]}-{parts[1]}", "-".join(parts[2:])
    return stem, ""


def cmd_checkpoint(args) -> int:
    state = load_state()
    name = resolve_name(state, args.name)
    require_workspace(state, name)

    if args.list:
        d = ckpt_dir(name)
        files = sorted(d.glob("*.npz")) if d.exists() else []
        if not files:
            eprint(f"[jspace] no checkpoints for '{name}'.")
            return 0
        print(f"{'file':<34} {'timestamp':<17} {'label':<16} nodes")
        for f in files:
            ts, label = _parse_ckpt_name(f.name)
            try:
                data = np.load(f, allow_pickle=False)
                nodes = int(data["vocab"].shape[0])
            except Exception:  # noqa: BLE001
                nodes = -1
            print(f"{f.name:<34} {ts:<17} {label:<16} {nodes}")
        return 0

    if args.restore:
        src = Path(args.restore)
        if not src.exists():
            cand = ckpt_dir(name) / args.restore
            if cand.exists():
                src = cand
            else:
                die(f"checkpoint file not found: {args.restore}")
        data = np.load(src, allow_pickle=False)
        vocab = np.array(data["vocab"])
        save_matrix(name, [str(x) for x in vocab.tolist()], data["W"], data["E"], data["a"])
        entry = json.loads(str(data["state_json"].item())) if "state_json" in data else default_ws_entry()
        entry["activation"] = [float(x) for x in data["a"]]
        entry["updated"] = now_iso()
        state["workspaces"][name] = entry
        state["current"] = name
        save_state(state)
        graph = None
        if "graph_json" in data:
            try:
                graph = json.loads(str(data["graph_json"].item()))
            except Exception:  # noqa: BLE001
                graph = None
        if graph and graph.get("nodes"):
            atomic_write_json(graph_path(name), graph)
        else:
            graph = json.loads(graph_path(name).read_text()) if graph_path(name).exists() else {"nodes": []}
        atomic_write_text(
            digest_path(name),
            render_digest(name, graph, [str(x) for x in vocab.tolist()], data["W"], data["a"]),
        )
        eprint(f"[jspace] restored '{name}' from {src.name}.")
        print(f"restored: {src}")
        return 0

    dest = _save_checkpoint(name, args.label, state)
    eprint(f"[jspace] checkpointed '{name}' -> {dest}")
    print(str(dest))
    return 0


# --------------------------------------------------------------------------- #
# subcommand: hypnotize
# --------------------------------------------------------------------------- #

def _parse_trigger(spec: str) -> dict:
    if "=>" not in spec:
        die(f"--trigger expects 'X => Y[,Z]', got {spec!r}")
    when, rhs = spec.split("=>", 1)
    when = when.strip()
    lights = [w.strip() for w in rhs.split(",") if w.strip()]
    if not when or not lights:
        die(f"--trigger needs a 'when' and at least one target: {spec!r}")
    return {"when": when, "light": lights}


def _parse_pin(spec: str) -> tuple[str, float]:
    if ":" in spec:
        word, strength = spec.rsplit(":", 1)
        try:
            return word.strip(), max(0.0, min(1.0, float(strength)))
        except ValueError:
            die(f"--pin strength must be a number, got {strength!r}")
    return spec.strip(), 0.9


def render_induction(name: str, entry: dict, graph: dict, script: str | None) -> str:
    note_of = {n["word"]: n.get("note") for n in graph.get("nodes", [])}
    pins = entry.get("pins", {})
    triggers = entry.get("triggers", [])
    suppress = entry.get("suppress", [])

    out = [f"# J-space induction: {name}", ""]
    out.append("While working in this project, keep these concepts active in your "
               "thinking, even when they are not named in the prompt:")
    out.append("")
    if pins:
        for w, strength in sorted(pins.items(), key=lambda kv: -kv[1]):
            note = note_of.get(w)
            line = f"- **{w}** (hold at {float(strength):.2f})"
            if note:
                line += f" — {note}"
            out.append(line)
    else:
        out.append("- _(no pinned concepts)_")
    out.append("")
    if triggers:
        out.append("Associations to make automatically:")
        out.append("")
        for rule in triggers:
            lights = ", ".join(f"`{w}`" for w in rule["light"])
            out.append(f"- When `{rule['when']}` comes up, associate {lights}.")
        out.append("")
    if suppress:
        out.append("Try to keep these out of focus (soft - naming them can "
                   "paradoxically surface them):")
        out.append("")
        for w in suppress:
            out.append(f"- Avoid dwelling on `{w}`.")
        out.append("")
    if script:
        out.append(script.strip())
        out.append("")
    return "\n".join(out)


def cmd_hypnotize(args) -> int:
    state = load_state()
    name = resolve_name(state, args.name)
    entry = require_workspace(state, name)
    vocab, _W, _E, _a = load_matrix(name)
    vset = set(vocab)

    if not (args.pin or args.trigger or args.suppress or args.script):
        die("nothing to install (use --pin/--trigger/--suppress/--script).")

    # 1) always checkpoint the pre-hypnosis state first
    ckpt = _save_checkpoint(name, "pre-hypnosis", state)

    # 2) validate every referenced word exists
    new_pins = dict(_parse_pin(p) for p in (args.pin or []))
    new_triggers = [_parse_trigger(t) for t in (args.trigger or [])]
    new_suppress = [w.strip() for w in (args.suppress or [])]
    referenced = set(new_pins) | set(new_suppress)
    for rule in new_triggers:
        referenced.add(rule["when"])
        referenced.update(rule["light"])
    unknown = sorted(w for w in referenced if w not in vset)
    if unknown:
        die(f"these concepts are not in the workspace vocab: {', '.join(unknown)}")

    # 3) merge into the workspace entry
    pins = entry.setdefault("pins", {})
    pins.update(new_pins)
    triggers = entry.setdefault("triggers", [])
    for rule in new_triggers:
        merged = False
        for existing in triggers:
            if existing["when"] == rule["when"]:
                for w in rule["light"]:
                    if w not in existing["light"]:
                        existing["light"].append(w)
                merged = True
                break
        if not merged:
            triggers.append(rule)
    suppress = entry.setdefault("suppress", [])
    for w in new_suppress:
        if w not in suppress:
            suppress.append(w)
    entry["updated"] = now_iso()
    state["workspaces"][name] = entry
    save_state(state)

    # 4) render the induction artifact (+ optional always-on rule)
    graph = json.loads(graph_path(name).read_text()) if graph_path(name).exists() else {"nodes": []}
    induction = render_induction(name, entry, graph, args.script)
    atomic_write_text(induction_path(name), induction)
    if args.install_rule:
        mdc = (
            "---\n"
            "alwaysApply: true\n"
            f"description: J-space induction for {name}\n"
            "---\n\n"
            + induction
        )
        atomic_write_text(rule_path(name), mdc)

    eprint(f"[jspace] hypnotized '{name}'. pre-hypnosis checkpoint: {ckpt}")
    print(f"workspace: {name}")
    if new_pins:
        print("pinned: " + ", ".join(f"{w}={s:.2f}" for w, s in new_pins.items()))
    if new_triggers:
        for rule in new_triggers:
            print(f"trigger: {rule['when']} => {', '.join(rule['light'])}")
    if new_suppress:
        print("suppressed: " + ", ".join(new_suppress))
    print(f"induction: {induction_path(name)}")
    if args.install_rule:
        print(f"rule: {rule_path(name)}")
    print(f"checkpoint: {ckpt}")
    print("`wake` restores this state.")
    return 0


# --------------------------------------------------------------------------- #
# subcommand: wake
# --------------------------------------------------------------------------- #

def _latest_pre_hypnosis(name: str) -> Path | None:
    d = ckpt_dir(name)
    if not d.exists():
        return None
    cands = []
    for f in d.glob("*.npz"):
        _ts, label = _parse_ckpt_name(f.name)
        if label == "pre-hypnosis":
            cands.append(f)
    return sorted(cands)[-1] if cands else None


def cmd_wake(args) -> int:
    state = load_state()
    name = resolve_name(state, args.name)
    entry = require_workspace(state, name)
    only = args.only
    cleared: list[str] = []

    if only in (None, "pins"):
        if entry.get("pins"):
            cleared.append(f"{len(entry['pins'])} pin(s)")
        entry["pins"] = {}
    if only in (None, "triggers"):
        if entry.get("triggers"):
            cleared.append(f"{len(entry['triggers'])} trigger(s)")
        entry["triggers"] = []
    if only in (None, "suppressions"):
        if entry.get("suppress"):
            cleared.append(f"{len(entry['suppress'])} suppression(s)")
        entry["suppress"] = []

    # remove the installed rule file unless pins/triggers remain
    rp = rule_path(name)
    keep_rule = bool(entry.get("pins") or entry.get("triggers"))
    if rp.exists() and not keep_rule:
        rp.unlink()
        cleared.append("installed rule")

    restored_from = None
    if only is None:
        ckpt = _latest_pre_hypnosis(name)
        if ckpt is not None:
            data = np.load(ckpt, allow_pickle=False)
            if int(data["a"].shape[0]) == len(entry.get("activation", [])) or not entry.get("activation"):
                entry["activation"] = [float(x) for x in data["a"]]
                restored_from = ckpt.name
            else:
                entry["activation"] = [float(x) for x in data["a"]]
                restored_from = ckpt.name

    entry["updated"] = now_iso()
    state["workspaces"][name] = entry
    save_state(state)

    if not cleared:
        eprint(f"[jspace] nothing to clear on '{name}'.")
    else:
        eprint(f"[jspace] woke '{name}': cleared {', '.join(cleared)}.")
    print(f"workspace: {name}")
    print("cleared: " + (", ".join(cleared) if cleared else "(nothing)"))
    if restored_from:
        print(f"activation restored from: {restored_from}")
    elif only is None:
        print("activation restored from: (no pre-hypnosis checkpoint; kept live activation)")
    return 0


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="jspace",
        description="External mental workspace (J-space) for a coding agent.",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="draft a graph.json from a codebase")
    p_scan.add_argument("dir", help="directory to scan")
    p_scan.add_argument("--name", required=True, help="workspace name")
    p_scan.add_argument("--top", type=int, default=120, help="max concepts to keep")
    p_scan.add_argument("--force", action="store_true", help="overwrite an existing graph.json")
    p_scan.set_defaults(func=cmd_scan)

    p_build = sub.add_parser("build", help="compile graph.json -> matrix + digest")
    p_build.add_argument("name", help="workspace name")
    p_build.add_argument("--sim-threshold", type=float, default=0.35,
                         help="cosine threshold for similarity edges (default 0.35)")
    p_build.add_argument("--alpha", type=float, default=0.75,
                         help="spreading-activation decay used by query (default 0.75)")
    p_build.set_defaults(func=cmd_build)

    p_load = sub.add_parser("load", help="print the digest + currently active concepts")
    p_load.add_argument("name", nargs="?", help="workspace name (default: current)")
    p_load.set_defaults(func=cmd_load)

    p_query = sub.add_parser("query", help="spreading activation from a text query")
    p_query.add_argument("text", help="query text")
    p_query.add_argument("--name", help="workspace name (default: current)")
    p_query.add_argument("--top", type=int, default=20, help="how many concepts to print")
    p_query.add_argument("--iterations", type=int, default=5, help="spreading iterations")
    p_query.set_defaults(func=cmd_query)

    p_edit = sub.add_parser("edit", help="mutate the graph and rebuild")
    p_edit.add_argument("--name", help="workspace name (default: current)")
    p_edit.add_argument("--add", action="append", metavar="word:weight[:note]")
    p_edit.add_argument("--link", action="append", metavar="a:b:w")
    p_edit.add_argument("--boost", action="append", metavar="word")
    p_edit.add_argument("--lower", action="append", metavar="word")
    p_edit.add_argument("--remove", action="append", metavar="word")
    p_edit.set_defaults(func=cmd_edit)

    p_ckpt = sub.add_parser("checkpoint", help="save / list / restore snapshots")
    p_ckpt.add_argument("--name", help="workspace name (default: current)")
    p_ckpt.add_argument("--label", default="manual", help="label for a new checkpoint")
    p_ckpt.add_argument("--list", action="store_true", help="list existing checkpoints")
    p_ckpt.add_argument("--restore", metavar="FILE", help="restore from a checkpoint file")
    p_ckpt.set_defaults(func=cmd_checkpoint)

    p_hyp = sub.add_parser("hypnotize", help="install pins / triggers / suppressions")
    p_hyp.add_argument("--name", help="workspace name (default: current)")
    p_hyp.add_argument("--pin", action="append", metavar="word[:strength]")
    p_hyp.add_argument("--trigger", action="append", metavar="'X => Y[,Z]'")
    p_hyp.add_argument("--suppress", action="append", metavar="word")
    p_hyp.add_argument("--script", help="free-text suggestion included verbatim")
    p_hyp.add_argument("--install-rule", action="store_true",
                       help="also write .cursor/rules/jspace-<name>.mdc (alwaysApply)")
    p_hyp.set_defaults(func=cmd_hypnotize)

    p_wake = sub.add_parser("wake", help="lift the induction, restore pre-hypnosis activation")
    p_wake.add_argument("--name", help="workspace name (default: current)")
    p_wake.add_argument("--only", choices=["pins", "triggers", "suppressions"],
                        help="clear just one category")
    p_wake.set_defaults(func=cmd_wake)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
