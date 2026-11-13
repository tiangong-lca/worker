---
title: Local Model Runtime Release
docType: runbook
scope: repo
status: active
authoritative: false
owner: worker
language: en
whenToUse:
  - when qualifying or publishing downloadable local Model runtimes
whenToUpdate:
  - when native release artifacts, verification or publication steps change
checkPaths:
  - .github/workflows/model-runtime.yml
  - model-runtime-version.txt
  - scripts/collect_model_release_sources.py
  - scripts/verify_model_release_assets.py
  - scripts/package_model_runtime.py
lastReviewedAt: 2026-11-13
lastReviewedCommit: 28c170608d5a1cac46d0f16365ab40cf9a87be86
related:
  - repo-validation.md
  - contracts/study-compiler-contract.md
---

# Local Model runtime release

The `Local Model native qualification` workflow builds the explicit local Model
engine on macOS ARM64, Ubuntu 24.04 x64/ARM64 and Windows x64. Linux packages
require glibc 2.39 or newer. macOS Intel, Linux musl and Windows ARM64 are excluded.
It neither deploys a server Worker nor enables hosted Study capabilities.

The source version is in `model-runtime-version.txt`. Change it through a reviewed
PR before publishing another version. Run `model-runtime.yml` on a candidate branch
with `publish=false` first. Publication requires a reviewed Main commit and an
explicit dispatch with `publish=true`; all four native jobs must succeed before
the single publication job can create `model-runtime-v<version>`.

Each native job builds with Cargo’s optimized release profile and packages its
own binary, native dependencies and the pinned LCIA
cache, removes developer library search paths, extracts the package into a new
directory and checks independent numerical expectations of 21 and 42. These are
synthetic two-process qualification cases using actual factors, not scientific
validation of a user's research. A failed platform prevents the entire release.

The embedded reviewed LCIA manifest retains exact Git bytes on every host through
`.gitattributes`; Windows newline conversion must not change its content identity.
Packaged-engine self-check failures retain the native diagnostic in the job log.

Release collection requires Python 3.12 or newer. Corresponding source includes
the exact Worker tree, locked vendored Rust dependencies and their notices,
native source archives and build recipes. Linux uses the installed packages'
exact source versions and includes the common license texts referenced by Debian
copyright notices; macOS refuses an installed formula/source recipe mismatch;
Windows carries the pinned vcpkg recipes, downloaded sources, component notices
and Microsoft-published runtime/Build Tools license terms and redistribution list. The source archive and `components.json`
retain the component licenses and versions; component licenses remain in force.

Each platform also installs the selected public CLI version with Node 24.19.0,
uses isolated state outside the source checkout to install the package, prepares
a Model, calculates both demands, reads all result kinds and renders an HTML
report. Both engine and installed-consumer checks must pass.

Each platform publishes five assets:

- `model-runtime-<platform>.tar.gz`: outer package containing `manifest.json` and
  `runtime.tar.gz`, ready for CLI installation after extraction.
- `model-runtime-<platform>-sources.tar.gz`: corresponding sources and notices.
- `model-runtime-<platform>-qualification.json`: actual native numerical receipt.
- `model-runtime-<platform>-cli-qualification.json`: public installed CLI operations
  and numerical/report verification.
- `model-runtime-<platform>-provenance.json`: source, workflow run, host and method
  cache identity. This is a workflow receipt, not a signed attestation.

`SHA256SUMS` covers every asset. The publication verifier rejects incomplete
platform sets, wrong source/platform receipts, incorrect numeric results,
archive-integrity mismatch and missing corresponding-source material. A draft
release is made public only after upload completes. Existing versions are not
overwritten; inspect a failed draft and its original run before recovery.

Download the asset matching the host from the public GitHub release and verify
its entry in `SHA256SUMS`. Extract it into a new directory, then use a released
CLI with Model support:

```sh
tiangong-lca setup model-calculation --install /path/to/extracted-package --json
tiangong-lca setup model-calculation --check --json
tiangong-lca dataset model calculate --file input.json --output-dir run-1 --json
```

The CLI verifies the inner archive and every extracted file, checks the actual
engine/cache and saves local configuration. No database, queue or login is needed
for local execution. Verify installation, calculation and report generation from
a public CLI install outside the source checkout before closing release delivery.
