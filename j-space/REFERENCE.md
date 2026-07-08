# j-space - Reference

Deep reference for the local j-space skill: the data model, the spreading-
activation math, tuning, the hypnosis mechanics, checkpoints, and troubleshooting.

## The model: all-MiniLM-L6-v2

- A small sentence-transformer (6-layer MiniLM) producing **384-dim** embeddings.
- Public and ungated - downloads anonymously, no Hugging Face account or token.
- Runs on **Apple MPS** when available, otherwise **CPU**. Works on any platform;
  unlike the repo's MLX-only media skills, there is no Apple-Silicon requirement.
- Cached under `~/.j-space/models/` (override with `JSPACE_HOME`). Loaded once per
  CLI invocation; `build`/`query`/`edit` pay a ~2-7s model-load cost per call.

## Files and layout

```
# outside any repo - the runtime only
~/.j-space/
├── .venv/            # uv venv (numpy + sentence-transformers)
└── models/           # cached all-MiniLM-L6-v2

# inside the analyzed project - the data (safe to commit)
./.jspace/
├── state.json                 # workspace registry + live activation + hypnosis
└── <name>/
    ├── graph.json             # editable source of truth: nodes + edges
    ├── matrix.npz             # compiled: vocab, W, E, a
    ├── digest.md              # rendered active-memory summary (what `load` prints)
    ├── induction.md           # hypnosis priming block (if hypnotized)
    └── checkpoints/
        └── <YYYYMMDD-HHMMSS>-<label>.npz
```

An installed induction rule (from `hypnotize --install-rule`) is written outside
`.jspace/` at `./.cursor/rules/jspace-<name>.mdc`.

## matrix.npz format

A NumPy `.npz` with four aligned arrays (row `i` is the same concept in all):

| Array | Shape | Meaning |
|-------|-------|---------|
| `vocab` | `(N,)` unicode | concept words, the canonical order |
| `W` | `(N, N)` float32 | row-normalized adjacency (each row sums to 1, or 0 if isolated) |
| `E` | `(N, 384)` float32 | unit-normalized concept embeddings |
| `a` | `(N,)` float32 | baseline activation, seeded from node weights, max-scaled to 1.0 |

`W` is built by combining explicit edges (from `graph.json`) with cosine-
similarity edges, symmetrizing, zeroing the diagonal, then row-normalizing.
Explicit edges win over similarity edges for the same pair. A node's row in `W` is
its outgoing transition distribution used by spreading activation.

The **live** activation (updated by each `query`) lives in `state.json`, not in
`matrix.npz`; the `a` in `matrix.npz` is the post-build baseline.

## Build: from graph to matrix

1. **Validate** `graph.json`: reject duplicate node words and edges that reference
   unknown words (exit 1 naming the offender).
2. **Embed** each node. The embedded string is `"word — note"` when a note exists,
   else just `word`, so notes disambiguate short concepts.
3. **Adjacency.** Insert explicit edges. Then for every pair with cosine
   `>= sim-threshold` and no explicit edge, add a similarity edge with weight equal
   to the cosine. Symmetrize, zero the diagonal, row-normalize -> `W`.
4. **Activation seed.** `a = weights / max(weights)`.
5. Write `matrix.npz`, render `digest.md`, register in `state.json`, set `current`.

`--sim-threshold` (default **0.35**) controls graph density: lower = more
similarity edges (denser, more spreading); higher = sparser, more literal.

## Spreading activation (query)

A query embeds the text, seeds the concepts it is "about", then diffuses that
energy across the graph:

```
seed s:  the strongest-matching concepts get their cosine value; others 0
          (nodes with cosine >= max(0.2, 0.85 * best_cosine), capped at 8)

iterate k times (default k = 5):
    a  <-  alpha * (W . a)  +  (1 - alpha) * s
    a  <-  a / max(a)                     # renormalize peak to 1.0

then apply hypnosis, in order:
    suppress:  a[w] *= 0.2                for each suppressed w
    pins:      a[w]  = max(a[w], strength) for each pinned w
    triggers:  if a[when] >= 0.2:         for each trigger rule
                   a[light] = max(a[light], 0.8)
```

- **`alpha`** (default **0.75**) is the balance between association and the query.
  Higher alpha = more spreading (the graph's structure dominates, more `[assoc]`
  discovery, more "drift"); lower alpha = the query text dominates (more literal,
  less exploration). Set at `build` time, stored per workspace, reused by `query`.
- **`--iterations`** (default 5): more iterations spread activation further from
  the seeds. 3-8 is the useful range.
- Because the starting `a` is the **live** activation from the previous query,
  concepts stay partly lit across queries - this is the intended continuity, the
  workspace "keeping things on its mind." Re-run `build` (or `checkpoint --restore`)
  to reset to baseline.

**Provenance tags** in the output: `[seed]` (matched the query text), `[assoc]`
(reached by spreading), `[pin]` / `[trigger]` (forced by hypnosis). Precedence when
a concept qualifies for several: pin > trigger > seed > assoc.

## The digest

`digest.md` is what `load` prints for injection into your context:

- **Top concepts** - the 20 highest-weight nodes with notes.
- **Clusters** - connected components of `W` thresholded at 0.15, largest first
  (up to 8), each a comma-separated group.
- **Thought sequence** - a greedy activation-weighted traversal: start at the
  highest-activation concept, hop to the strongest unvisited neighbor, restart at
  the next-highest when stuck. This is the "thought-sequence keywords" chain -
  a linear reading order through the workspace.

## Incremental rebuild (edit)

`edit` mutates `graph.json` then rebuilds. It loads the previous `matrix.npz` and
reuses the embedding row for any concept whose embedded string (`word` + `note`)
is unchanged, so only new or changed concepts are re-embedded. Removing a concept
also drops its edges and any pins/triggers/suppressions that reference it.

## Checkpoints

Each checkpoint `.npz` is **self-contained**: it stores `vocab`, `W`, `E`, `a`
(the live activation at checkpoint time), plus `state_json` (the workspace's
`state.json` entry) and `graph_json` (the full graph). `--restore` overwrites
`matrix.npz`, the `state.json` entry, and `graph.json`, then regenerates
`digest.md` - so a restore is exact and needs no other file.

`hypnotize` always writes an automatic checkpoint labeled `pre-hypnosis` before
applying anything, which is what `wake` restores activation from.

## Hypnosis mechanics

The hypnosis metaphor is implemented entirely as durable state + optional context
injection - there is no model-internal manipulation:

- **pins** (`state.json.pins`: `word -> strength`) are re-applied at the end of
  every `query`, so a pinned concept never falls out of the lit-up set.
- **triggers** (`state.json.triggers`) are post-hypnotic associations: `when` a
  concept is active (>= 0.2), its `light` concepts are forced to >= 0.8.
- **suppressions** (`state.json.suppress`) multiply a concept's activation by 0.2.
  This is **soft** on purpose: as in the research's "white-bear" result, naming a
  concept to avoid it can partly surface it, so suppression nudges rather than
  guarantees absence.
- **induction.md** renders these as an imperatively-worded priming block. With
  `--install-rule` it is also written as `.cursor/rules/jspace-<name>.mdc` with
  `alwaysApply: true`, so it enters your context every session automatically -
  the closest prompt-side analog to a persistent "thought on the mind."

`wake` clears the categories (all, or `--only` one), deletes the installed rule
(unless pins/triggers remain), and restores activation from the latest
`pre-hypnosis` checkpoint.

## Verification / smoke test

The behavior this skill must preserve (run in a scratch project):

1. Author a ~10-node `graph.json`, `build` it -> `matrix.npz` exists, every `W`
   row sums to 1 (or 0), `digest.md` shows clusters and a thought sequence.
2. `query` near one concept -> it appears `[seed]`; its explicit graph neighbors
   light up as `[assoc]`.
3. Two more unrelated queries -> earlier concepts decay but do not vanish from
   the activation vector.
4. `hypnotize --pin X --trigger "A => B" --install-rule` -> `induction.md` and the
   rule file exist; a query mentioning A shows B `[trigger]`; X shows `[pin]`
   across 5 consecutive queries.
5. `wake` -> rule file gone, pins/triggers empty, activation equals the
   `pre-hypnosis` checkpoint's.
6. `checkpoint --restore` of an older checkpoint round-trips `vocab`/`W`/`E`/`a`
   exactly.

## Troubleshooting

- **`sentence-transformers not available`** - run `scripts/setup_env.sh` and call
  `jspace.py` with the venv python (`~/.j-space/.venv/bin/python`).
- **Model download failed at setup** - the model is *public*; this is almost
  always a network/proxy issue, not auth. Re-run `setup_env.sh`, or run once with
  network so it downloads lazily on the first `build`.
- **Slow queries** - each invocation reloads the embedding model (~2-7s). This is
  expected; batch your thinking into fewer `query` calls, or keep sessions warm.
- **`unknown workspace` (exit 2)** - you have no `current` workspace or named one
  that was never built. Run `build <name>` or pass an explicit name.
- **Everything shows `[seed]` / nothing shows `[assoc]`** - your graph is too
  small or too densely similar. Add explicit edges, or lower `--sim-threshold`
  at build and raise `alpha` so spreading dominates.
- **MPS errors** - the CLI falls back to CPU automatically; if a torch/MPS build
  misbehaves, set `PYTORCH_ENABLE_MPS_FALLBACK=1` or run on CPU.
- **Committing the wrong thing** - never commit `~/.j-space/`. The `./.jspace/`
  data is yours to commit or `.gitignore` as you prefer.
