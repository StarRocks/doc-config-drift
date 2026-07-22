# doc-config-drift

Detect drift between StarRocks **configuration code** (the source of truth) and
its **documentation** — wrong documented defaults, wrong mutability, missing
defaults, and stale/undocumented parameters.

A documented default that contradicts the code (e.g. `slow_query_analyze_threshold`
documented as `5` when the code sets `5000`) silently misconfigures clusters and
generates support load. This tool finds those, repeatably, every release.

## Design principle

**Extraction is deterministic on both sides — no LLM.** A finding is never
hallucinated; every one is a literal, reproducible diff between two files. Use an
LLM only *downstream*, to draft the doc edits this tool proposes.

Single file, standard library only (`drift_audit.py`), so it can be **vendored**
into any repo — public OSS or private — with nothing to install.

## What it checks

| Source of truth | Docs |
|---|---|
| FE: `fe/.../common/Config.java` (`@ConfField`) | `docs/en/administration/management/FE_parameters/*.md` |
| BE: `be/src/common/config.h` (`CONF_[m]Type(name,"default")`) | `docs/en/administration/management/BE_parameters/*.md` |

| Finding | Meaning | Action |
|---|---|---|
| `default_mismatch` | Documented default contradicts code | Fix docs (or code, if code is wrong) |
| `mutable_mismatch` | Documented "Is mutable" contradicts code | Fix docs |
| `doc_missing_default` | Doc block has no/empty `Default:` | Fill it in |
| `stale_in_docs` | Documented, not in code | Remove or mark deprecated |
| `undocumented` | In code, not in docs | Triage (many are intentionally internal) |

## Usage

```bash
# Audit a local StarRocks checkout
python3 drift_audit.py --component fe --repo /path/to/starrocks
python3 drift_audit.py --component be --repo /path/to/starrocks --format markdown

# CI: report only params this PR touched
python3 drift_audit.py --component be --repo . --changed-since origin/main

# Split-repo: code from StarRocks, docs from another repo (e.g. commercial docs)
python3 drift_audit.py --component fe \
    --code-file /path/to/starrocks/fe/.../Config.java \
    --docs-dir  /path/to/phoenixai-docs/.../FE_parameters
```

`--format` is `text` (default), `json`, or `markdown`. Exit code is non-zero when
authoritative drift exists (tune with `--fail-on`), so it works directly as a gate.

## CI (reusable workflow)

Consuming repos call the reusable workflow. Same-repo case (code + docs together):

```yaml
# .github/workflows/doc-drift.yml in StarRocks/starrocks
on: [pull_request]
jobs:
  drift:
    uses: StarRocks/doc-config-drift/.github/workflows/reusable-drift-check.yml@main
    with:
      components: "fe be"
      changed-since: ${{ github.event.pull_request.base.sha }}
```

Split-repo case (docs repo, code lives in StarRocks/starrocks) — pass `code-repo`;
see `.github/workflows/reusable-drift-check.yml` for all inputs. Results are written
to the job's Step Summary.

## Consumers

- **StarRocks/starrocks** — see note below.
- **PhoenixAI commercial docs** (`phoenixdata-docs`, …) —
  run against the StarRocks codebase (which they build on) with their own
  `--docs-dir`.

### Relationship to `StarRocks/starrocks`'s `extract_and_diff_params.py`

That in-repo tool detects *new undocumented* params (by name) and drafts AI
descriptions for the "Docs needed:" issues. It historically did **not** compare
documented **values**. Two integration options (project decision):

1. **Vendor** this engine's value-drift logic into that tool (single file, no
   external dependency — friendliest for public OSS), keeping this repo the
   canonical source that commercial docs also use.
2. **Call** this repo's reusable workflow from StarRocks CI directly.

## Parser gotchas (all regression-tested in `tests/`)

These caused false findings during development and must never regress:

1. Rendered vs source Markdown bullets: `* Default:` vs `- Default:` — both accepted.
2. Semicolons inside `@ConfField(comment="…;…")` — find `public static` first,
   then the terminating `;`.
3. `CONF_Alias(primary, alias)` is not a declaration and has no string default —
   excluded, so it can't clobber the real default.
4. `CONF_Strings` / `CONF_String_enum` are real declarations — included.
5. Full-width colon `：` (from CJK input) in doc bullets — tolerated.
6. Arithmetic / `L` suffixes / `_` separators — evaluated, so `2 * 3600` == `7200L`.
   Single-element array notation (`query` == `{"query"}`) is not flagged.

## Development

```bash
python3 -m pytest tests/ -q
```
