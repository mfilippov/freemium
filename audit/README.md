# Network request audit

Finds code in the Chromium checkout that can send network requests without an
explicit user action, beyond what our patches already disable. Analysis runs on
a local model (llama.cpp); no source code leaves the machine.

Scripts are stdlib-only Python 3.11+, e.g. `uv run --no-project python audit/<script>.py`.

## Pipeline

1. `extract.py`: deterministic prefilter. Collects every traffic annotation
   (Chromium requires one per request), every unannotated request and every
   non-benign URL literal, groups them by enclosing function or constant, and
   attaches context (code, use sites elsewhere, our patch diff for the file).
   Output: `build/audit/<version>/candidates.jsonl`.
2. `serve.py <profile>` + `triage.py --model <profile>`: asks the model for a
   structured verdict per candidate (`prompt.md`, JSON schema in `triage.py`).
   Verdicts go to `findings/<version>/<profile>.jsonl`, which is committed and
   also serves as the cache: unchanged code is never re-analyzed.
3. `report.py --model <profile>`: lists background requests without a decision
   in `decisions.toml` into `build/audit/<version>/report.md`.

## Gates

Most false positives are requests guarded far from the sending code by checks
that are always false here (no API key, no signed-in account, no metrics
consent in non-branded builds). Two layers show them to the model:

- automatic: `extract.py` finds such checks (`GATES` in extract.py) in the
  candidate, in functions it calls and in its callers, and lists them under
  "GATES SEEN NEAR THIS CODE";
- verified: `gates.toml` holds mechanism-wide facts confirmed in code (e.g. by
  a verifier pass with a stronger model, `findings/<v>/verify.jsonl`); they
  are attached to every candidate under the listed paths. `check` entries cite
  the code; extract.py stops when they no longer match, so stale facts get
  re-verified on a new version.

`report.py --hide-gated` hides items a verifier cleared with a cited gate or
evidence, and items a `gates.toml` entry closes.

## New Chromium version

```
python apply-patches.py                       # patches applied first
python audit/extract.py
python audit/serve.py gemma4-fast              # in another shell
python audit/triage.py --model gemma4-fast     # only changed candidates are asked
python audit/report.py --model gemma4-fast
```

Then for each report item: write a patch and record `patched`, or record
`accepted` / `false_positive` in `decisions.toml`.

## Choosing or changing a model or the prompt

`eval.py build` labels candidates in the pristine tag: positives are code that
ungoogled-chromium privacy patches modify plus a list of known Chrome
background requests; the rest is a fixed random sample ("others" are not
verified negatives: a flag there may be a real finding).

`bakeoff.py <profile>...` runs each profile on the quick set unattended
(starts and stops llama-server, waits for downloads); `status.py` shows
downloads, the running server, progress and the score table.

Recall on positives is the main metric; with ~35 positives the 95% interval is
about +-15 points. Tune the prompt on the fastest model first, then compare
models. "out tok" around 2k means the model is thinking.

`extract.py` fails if any desktop annotation from Chromium's annotations.xml
is not a candidate, so prefilter rules cannot silently drop real code.

## Blind spots

- Requests made by code paths without a URL literal or annotation in the same
  function (e.g. URL built from a pref) are only caught via their annotation.
- Field trials and features toggled remotely are not evaluated per feature.
- Verify with a runtime check: run the build with `--log-net-log=netlog.json`
  in an idle profile and list contacted hosts with `netlog.py netlog.json`.
  157.0.8103.38, 10 min idle, fresh profile: only `wpad` DNS lookups (Windows
  proxy auto-detect, a system setting); after own/os-dns-and-no-system-proxy.patch: no hosts at all.
