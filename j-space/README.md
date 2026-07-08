# j-space

**An external, persistent "mental workspace" for your coding agent.**

j-space gives an AI agent something it normally lacks: a durable, inspectable set
of concepts it keeps "in mind" about a topic or a codebase - and that stays
consistent across turns and sessions. It's a small local tool (Python + a tiny
embedding model, no cloud, no API keys) that builds a weighted concept graph,
compiles it into a matrix, and lets the agent load, query, prime, and checkpoint
that workspace.

## The idea

In July 2026, Anthropic published
[*A global workspace in language models*](https://www.anthropic.com/research/global-workspace).
They found that Claude has a small collection of internal patterns - each tied to
a word - that play a special role: the model can **report** them, **control**
them, and **reason** with them, while the rest of its processing runs
automatically. They call it the **J-space** (after the "Jacobian lens" used to
find it). When "France" lights up in the J-space, France is *on the model's mind*
- not necessarily being said, but available to think with.

This skill is an **external, honest emulation** of that idea. It can't read or edit
a model's real internal activations. Instead it builds a *prompt-side* workspace:
a compact vocabulary of concepts with weighted connections and an activation
vector, which the agent "lights up" and loads into its context so it thinks about
your subject in richer, more consistent terms. Think of it as a **notebook the
agent keeps for one topic** - one it can query by association and be primed by.

## How it maps to the research

| In the paper (J-space) | In this skill |
|---|---|
| A word-linked internal pattern | A node in `graph.json` (a concept) |
| A concept being "on the mind" | High activation in the `a` vector |
| The J-lens readout (what's lit up) | `query` output (ranked lit-up concepts) |
| Reasoning by association / broadcast | Spreading activation across the graph `W` |
| Injecting a thought (e.g. "lightning") | `hypnotize --pin` (clamp a concept active) |
| Multi-step reasoning surfacing in order | The `digest.md` **thought sequence** |
| The "white-bear" / told-not-to-think effect | `--suppress` (deliberately *soft*) |
| Concepts staying available across a task | Live activation persisting across queries |

It's an analogy, not an equivalence - see [Honest caveats](#honest-caveats).

## Quickstart (about 5 minutes)

```bash
# 0. one-time setup (venv + a ~90 MB embedding model, no HF account)
bash scripts/setup_env.sh
PY="$HOME/.j-space/.venv/bin/python"
JS="scripts/jspace.py"          # path to this skill's CLI
```

Author a small `./.jspace/ocean/graph.json` (or let the agent build it from
research / a `scan`), then:

```bash
$PY $JS build ocean
```
```
workspace: ocean
nodes: 10
edges: 6 explicit, 4 similarity
digest: .jspace/ocean/digest.md
```

Ask what lights up:

```bash
$PY $JS query "the old lighthouse on the cliff"
```
```
## Lit up

- **lighthouse** (1.00) [seed]
- **beacon** (0.92) [assoc]
- **keeper** (0.92) [assoc]
- **harbor** (0.83) [assoc]
- **reef**  (0.75) [assoc]
...
## Related but unmentioned
beacon, keeper, harbor, reef, fog
```

`lighthouse` matched your text (`[seed]`); `beacon`, `keeper`, `harbor` lit up by
**association** (`[assoc]`) - the useful "you might also want to think about..."
leads. Run more queries and those associations carry forward.

## The hypnosis metaphor

A human hypnotist doesn't add knowledge - they hold a concept in the subject's
active mind and install "when X, then Y" suggestions. j-space does the analogous
thing to the *workspace*:

```bash
$PY $JS hypnotize --pin lighthouse --trigger "fog => foghorn" --install-rule
```

- **`--pin lighthouse`** keeps `lighthouse` lit in every query from now on.
- **`--trigger "fog => foghorn"`** force-lights `foghorn` whenever `fog` comes up.
- **`--install-rule`** writes `.cursor/rules/jspace-ocean.mdc` (an always-on
  rule) from the induction text, so the workspace primes the agent **every
  session automatically** - it doesn't have to consciously reload it.

`hypnotize` always takes a `pre-hypnosis` checkpoint first, so lifting it is safe:

```bash
$PY $JS wake        # clears pins/triggers, removes the rule, restores activation
```

## File-format tour

Everything is plain and inspectable (only `.npz` is binary), and lives in your
project under `./.jspace/` so it can travel with the repo:

- **`graph.json`** - the human-editable source of truth. `nodes` (each a `word`,
  a `weight` 0-1, and an optional `note`) and `edges` (`a`, `b`, a weight `w`, an
  optional `rel`). Edit this by hand anytime, then re-`build`.
- **`matrix.npz`** - the compiled workspace: `vocab` (concept order), `W` (the
  row-normalized connection matrix), `E` (384-dim embeddings), `a` (baseline
  activation). This is "the matrix" the whole skill runs on.
- **`digest.md`** - the rendered summary the agent loads: top concepts, clusters,
  and the thought-sequence keyword chain.
- **`induction.md`** - the priming block written when you hypnotize.
- **`checkpoints/*.npz`** - self-contained snapshots you can list and restore
  exactly.
- **`state.json`** - which workspace is current, the live activation, and the
  installed pins/triggers/suppressions.

The venv and embedding model live separately under `~/.j-space/` and must never
be committed.

## Honest caveats

- **It is context priming, not weight editing.** j-space cannot change model
  weights or literally inject internal activations. It builds a durable,
  inspectable prompt-side workspace. The value is real (consistency, recall of
  the right vocabulary, association discovery) but the mechanism is prompt-level.
- **Suppression is soft.** Telling the agent to avoid a concept can surface it -
  the "white-bear" effect. `--suppress` dampens, it doesn't guarantee absence.
- **A scan is a draft.** Codebase scanning gives you raw candidate terms; they
  need curation before the workspace is meaningful.

## FAQ / limits

- **How big should a workspace be?** A few dozen to a few hundred concepts. Short,
  distinct concepts work best (merge near-synonyms). Split large domains into
  several named workspaces.
- **When should I checkpoint?** Before a big edit or a hypnosis session, and
  whenever you reach a workspace state you'll want to return to. Restores are
  exact.
- **Does it need Apple Silicon?** No. It uses MPS if present, else CPU - any
  platform. (This is unusual for this repo, whose media skills are MLX-only.)
- **Is anything sent to the cloud?** No, not by the tool. Only the optional
  *topic research* step - which the agent runs, via the Perplexity MCP or web
  search - touches the network. Building and querying are fully local.

## More

- Agent usage and the full workflow: [SKILL.md](SKILL.md)
- Data model, the spreading-activation math, tuning, troubleshooting:
  [REFERENCE.md](REFERENCE.md)
- The research: [A global workspace in language models](https://www.anthropic.com/research/global-workspace)
