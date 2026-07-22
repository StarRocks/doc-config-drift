#!/usr/bin/env python3
"""
doc-config-drift — detect drift between StarRocks configuration CODE (the source
of truth) and its DOCUMENTATION.

Single file, standard library only, so it can be vendored into any repo — public
OSS or private — with no dependency to install.

  FE code: fe/.../common/Config.java   — `@ConfField` annotations
  BE code: be/src/common/config.h      — `CONF_[m]Type(name, "default")` macros
  Docs:    docs/en/administration/management/{FE,BE}_parameters/*.md
           (one `###` block per param, with `- Default:` / `- Is mutable:` bullets)

Design principle: EXTRACTION IS DETERMINISTIC on both sides — no LLM — so a
finding is never hallucinated; every one is a literal, reproducible diff. Use an
LLM only downstream, to draft the doc edits this tool proposes.

Findings:
  default_mismatch     documented default contradicts the code default
  mutable_mismatch     documented "Is mutable" contradicts the code
  doc_missing_default  doc block has no / empty Default line
  stale_in_docs        documented, but no longer present in code
  undocumented         present in code, absent from docs (often intentional)

Usage:
  # Audit a local checkout (CI / pre-commit):
  python3 drift_audit.py --component fe --repo /path/to/starrocks
  python3 drift_audit.py --component be --repo /path/to/starrocks --format markdown

  # Audit code from GitHub against docs in another repo (e.g. commercial docs):
  python3 drift_audit.py --component fe \\
      --code-file /path/to/Config.java \\
      --docs-dir  /path/to/celerdata-docs/en/.../FE_parameters

  # In CI, restrict findings to params touched by this PR:
  python3 drift_audit.py --component be --repo . --changed-since origin/main

Exit code is non-zero when authoritative contradictions exist
(default/mutability/stale/missing-default) so it works directly as a gate.
See --fail-on to tune which categories fail.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Default layout inside a StarRocks checkout. Override with --code-file/--docs-dir
# for repos with a different layout (e.g. commercial docs).
COMPONENTS = {
    "fe": {
        "code": "fe/fe-core/src/main/java/com/starrocks/common/Config.java",
        "docs": "docs/en/administration/management/FE_parameters",
        "kind": "java-conffield",
    },
    "be": {
        "code": "be/src/common/config.h",
        "docs": "docs/en/administration/management/BE_parameters",
        "kind": "cpp-conf",
    },
}
HARD_CATEGORIES = ("default_mismatch", "mutable_mismatch", "stale_in_docs",
                   "doc_missing_default")
RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"


# ── Code extraction: FE (@ConfField in Config.java) ──────────────────────────
def parse_code_java(text):
    params = {}
    hits = [m.start() for m in re.finditer(r"@ConfField\b", text)]
    for i, start in enumerate(hits):
        end = hits[i + 1] if i + 1 < len(hits) else len(text)
        region = text[start:end]
        # Find `public static` FIRST, then the terminating ';'. Order matters:
        # @ConfField(comment="…;…") puts semicolons inside the annotation string,
        # which must not be mistaken for the field's terminator.
        ps = re.search(r"public\s+static\s+", region)
        if not ps:
            continue
        anno, rest = region[:ps.start()], region[ps.end():]
        semi = rest.find(";")
        if semi == -1:
            continue
        d = re.match(r"(?:final\s+)?([\w\[\]<>]+)\s+(\w+)\s*(?:=\s*(.*))?;",
                     rest[:semi + 1].strip(), re.DOTALL)
        if not d:
            continue
        aliases = re.findall(r'"([^"]+)"', anno) if "aliases" in anno else []
        params[d.group(2)] = {
            "name": d.group(2), "type": d.group(1),
            "default": (d.group(3) or "").strip(),
            "mutable": bool(re.search(r"mutable\s*=\s*true", anno)),
            "aliases": aliases,
        }
    return params


# ── Code extraction: BE (CONF_ macros in config.h) ───────────────────────────
def parse_code_cpp(text):
    # Only real value-typed macros declare a param+default. Excludes CONF_Alias
    # (not a declaration; carries no string default that would clobber the real
    # entry). CONF_Strings / CONF_String_enum ARE real and must be included.
    types = r"Int16|Int32|Int64|Bool|Double|String_enum|Strings|String"  # longest first
    params = {}
    for m in re.finditer(rf"\bCONF_(m)?({types})\s*\(", text):
        mutable = m.group(1) == "m"          # CONF_mInt32(...) == mutable
        rest = text[m.end():]
        nm = re.match(r"\s*(\w+)\s*,", rest)
        if not nm:
            continue
        after = rest[nm.end():]
        dm = re.match(r'\s*"((?:[^"\\]|\\.)*)"', after)   # first string literal
        params[nm.group(1)] = {
            "name": nm.group(1), "type": m.group(2),
            "default": dm.group(1) if dm else "", "mutable": mutable, "aliases": [],
        }
    return params


CODE_PARSERS = {"java-conffield": parse_code_java, "cpp-conf": parse_code_cpp}


# ── Docs extraction ──────────────────────────────────────────────────────────
# Param heading is a single lowercase-first token, with (FE) or without (BE)
# backticks. Lowercase-first is what distinguishes a config param name from a
# section header ("## Logging", "## Query") — config names are always lowercase.
# h2–h6: StarRocks uses ### ; PhoenixData / cloud MDX partials use #####.
_DOC_HEADING_RE = re.compile(r"^#{2,6}\s+[`']?([a-z_][a-z0-9_.]*)[`']?\s*(?:\[.*)?$")
_FIELD_RE = {
    "default": re.compile(r"^[*-][ \t]*Default[:：][ \t]*(.*)$"),   # tolerate '*'/'-' and ：
    "mutable": re.compile(r"^[*-][ \t]*Is mutable[:：][ \t]*(.*)$"),
}


def parse_docs(md_paths):
    docs = {}
    for p in md_paths:
        text = Path(p).read_text()
        cur = None
        for line in text.splitlines():
            h = _DOC_HEADING_RE.match(line)
            if h:
                # regex matches single-token names only; multi-word section
                # headers ("## Query", "### Configure FE parameters") don't match.
                cur = h.group(1)
                docs.setdefault(cur, {"name": cur, "default": None,
                                      "mutable": None, "source_file": Path(p).name})
                continue
            if cur is None:
                continue
            for key, rx in _FIELD_RE.items():
                m = rx.match(line)
                if m and docs[cur][key] is None:
                    docs[cur][key] = m.group(1).strip()
    return docs


# ── Normalization ────────────────────────────────────────────────────────────
def norm_default(v):
    """Canonicalize so equivalent spellings compare equal: `2 * 3600` == 7200 ==
    `7200L`; `""` == "Empty string"; arrays by token set."""
    if v is None:
        return None
    s = v.replace("\\", "").strip()
    s = re.sub(r"`", "", s)
    s = re.sub(r"\(.*?\)", "", s)          # drop "(536870912)" / "(2 GB)" hints
    s = re.sub(r"//.*$", "", s).strip()
    num = re.sub(r"(?<=\d)[Ll]\b", "", s).replace("_", "")
    if re.fullmatch(r"[\d\s*+\-/().]+", num) and re.search(r"\d", num):
        try:
            return ("num", int(eval(num)))
        except Exception:
            pass
    if re.search(r"[{}]", s) or re.search(r"`[^`]+`\s*,", v):
        toks = frozenset(t for t in re.findall(r"[\w./-]+", s) if t)
        return ("empty",) if not toks else ("set", toks)
    s = s.strip('"').strip().lower()
    if s in ("", "{}", "-", "empty", "empty string", "an empty string", "none", "null"):
        return ("empty",)
    return ("str", re.sub(r"\s+", " ", s))


def doc_mutable(v):
    if v is None:
        return None
    v = v.strip().lower()
    if v.startswith(("y", "true")):
        return True
    if v.startswith(("n", "false")):
        return False
    return None


def _tokens(v):
    return frozenset(t.lower() for t in re.findall(r"[\w.%/-]+", v.replace("\\", "")))


# ── Diff ─────────────────────────────────────────────────────────────────────
def audit(code, docs, restrict=None, subset=False):
    # subset=True: the docs are a curated subset (e.g. a cloud product) — code
    # params they don't mention are intentional, so skip undocumented/stale and
    # report only value drift for the params they DO document.
    code_names = set(code)
    out = {k: [] for k in ("default_mismatch", "mutable_mismatch", "stale_in_docs",
                           "undocumented", "doc_missing_default")}
    for name, c in code.items():
        if restrict is not None and name not in restrict:
            continue
        d = docs.get(name) or next((docs[a] for a in c["aliases"] if a in docs), None)
        if d is None:
            if not subset:
                out["undocumented"].append(name)
            continue
        if not (d["default"] or "").strip():
            out["doc_missing_default"].append({"name": name, "file": d["source_file"]})
        else:
            cd, dd = c["default"], d["default"]
            if cd and not any(x in cd for x in ("getenv", "Config.", "System.", "(")):
                # token-set fallback suppresses pure notation diffs, e.g. a single
                # element array documented as a bare token: `query` == {"query"}.
                if norm_default(dd) != norm_default(cd) and _tokens(dd) != _tokens(cd):
                    out["default_mismatch"].append(
                        {"name": name, "doc": dd, "code": cd, "file": d["source_file"]})
        dm = doc_mutable(d["mutable"])
        if dm is not None and dm != c["mutable"]:
            out["mutable_mismatch"].append(
                {"name": name, "doc": d["mutable"],
                 "code": "Yes" if c["mutable"] else "No", "file": d["source_file"]})
    if restrict is None and not subset:
        for name, d in docs.items():
            if name not in code_names and not any(name in c["aliases"] for c in code.values()):
                out["stale_in_docs"].append({"name": name, "file": d["source_file"]})
    for k in ("default_mismatch", "mutable_mismatch", "stale_in_docs"):
        out[k].sort(key=lambda x: x["name"])
    out["undocumented"].sort()
    return out


# ── Restrict to params changed since a git ref (CI mode) ─────────────────────
def changed_param_names(repo, since):
    try:
        diff = subprocess.run(["git", "-C", str(repo), "diff", since],
                              capture_output=True, text=True, timeout=30).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    names = set()
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for m in re.finditer(r"public\s+static\s+(?:final\s+)?\S+\s+([a-z_][a-z0-9_]+)\s*=", line):
            names.add(m.group(1))
        for m in re.finditer(r"CONF_m?[A-Za-z0-9_]+\(\s*([a-z_][a-z0-9_]+)\s*,", line):
            names.add(m.group(1))
    return names


# ── Output ───────────────────────────────────────────────────────────────────
def render_markdown(component, label, code, docs, out):
    L = [f"# {component.upper()} configuration drift", "",
         f"- **Source:** {label}",
         f"- **Params in code:** {len(code)} &nbsp; **in docs:** {len(docs)}", "",
         "| Category | Count |", "|---|---|",
         f"| ❗ Default mismatch | {len(out['default_mismatch'])} |",
         f"| ❗ Mutability mismatch | {len(out['mutable_mismatch'])} |",
         f"| ❗ Doc missing Default | {len(out['doc_missing_default'])} |",
         f"| ⚠️ Stale in docs | {len(out['stale_in_docs'])} |",
         f"| ℹ️ Undocumented | {len(out['undocumented'])} |", ""]
    if out["default_mismatch"]:
        L += ["## Default value mismatches", "",
              "| Parameter | Docs say | Code says | Doc page |", "|---|---|---|---|",
              *[f"| `{x['name']}` | `{x['doc']}` | `{x['code']}` | {x['file']} |"
                for x in out["default_mismatch"]], ""]
    if out["mutable_mismatch"]:
        L += ["## Mutability mismatches", "",
              "| Parameter | Docs say | Code | Doc page |", "|---|---|---|---|",
              *[f"| `{x['name']}` | {x['doc']} | {x['code']} | {x['file']} |"
                for x in out["mutable_mismatch"]], ""]
    if out["doc_missing_default"]:
        L += ["## Docs missing a Default value", "",
              *[f"- `{x['name']}` ({x['file']})" for x in out["doc_missing_default"]], ""]
    if out["stale_in_docs"]:
        L += ["## Stale in docs (documented, not in code)", "",
              *[f"- `{x['name']}` ({x['file']})" for x in out["stale_in_docs"]], ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--component", choices=["fe", "be"], required=True)
    ap.add_argument("--repo", help="local StarRocks checkout (reads code + docs from it)")
    ap.add_argument("--source-repo", default="StarRocks/starrocks",
                    help="GitHub repo to fetch code+docs from when --repo is omitted")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--code-file", help="override: path to the code source-of-truth file")
    ap.add_argument("--docs-dir", help="override: directory of the *_parameters docs")
    ap.add_argument("--changed-since", help="restrict findings to params changed vs this git ref")
    ap.add_argument("--format", choices=["json", "markdown", "text"], default="text")
    ap.add_argument("--fail-on", default=",".join(HARD_CATEGORIES),
                    help="comma-separated categories that cause non-zero exit")
    ap.add_argument("--subset", action="store_true",
                    help="docs are a curated subset (e.g. a cloud product): report "
                         "only value drift, skip undocumented/stale")
    args = ap.parse_args()
    cfg = COMPONENTS[args.component]

    # Recursively collect .md and .mdx (cloud docs keep params in .mdx partials,
    # possibly nested under _assets/, cluster_management/, etc.).
    def doc_files(d):
        d = Path(d)
        return sorted(set(d.rglob("*.md")) | set(d.rglob("*.mdx")))

    # Resolve inputs
    if args.code_file and args.docs_dir:
        code_text = Path(args.code_file).read_text()
        doc_paths = doc_files(args.docs_dir)
        label = f"{args.code_file} vs {args.docs_dir}"
    elif args.repo:
        repo = Path(args.repo)
        code_text = (Path(args.code_file) if args.code_file else repo / cfg["code"]).read_text()
        docs_dir = Path(args.docs_dir) if args.docs_dir else repo / cfg["docs"]
        doc_paths = doc_files(docs_dir)
        label = f"{args.repo} @ {args.branch}"
    else:
        import urllib.request
        def fetch(path):
            url = RAW.format(repo=args.source_repo, branch=args.branch, path=path)
            return urllib.request.urlopen(url, timeout=30).read().decode()
        code_text = fetch(args.code_file or cfg["code"])
        # discovering doc pages remotely is repo-specific; require --repo/--docs-dir
        sys.exit("remote docs listing not supported; use --repo or --docs-dir")

    restrict = None
    if args.changed_since and args.repo:
        restrict = changed_param_names(args.repo, args.changed_since) or set()

    code = CODE_PARSERS[cfg["kind"]](code_text)
    docs = parse_docs(doc_paths)
    out = audit(code, docs, restrict, subset=args.subset)

    if args.format == "json":
        print(json.dumps({"component": args.component, "source": label,
                          "code_params": len(code), "doc_params": len(docs),
                          **out}, indent=2))
    elif args.format == "markdown":
        print(render_markdown(args.component, label, code, docs, out))
    else:
        for k in HARD_CATEGORIES:
            print(f"{k}: {len(out[k])}")
        print(f"undocumented: {len(out['undocumented'])}")

    fail_on = {c.strip() for c in args.fail_on.split(",") if c.strip()}
    sys.exit(1 if any(out[c] for c in fail_on) else 0)


if __name__ == "__main__":
    main()
