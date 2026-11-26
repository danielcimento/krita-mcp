# Docs

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | Components, request lifecycle, threading, how to add a command |
| [krita-api-notes.md](krita-api-notes.md) | Krita Python API facts and gotchas, marked verified or to verify |
| [f001_buildout/](f001_buildout/) | Buildout of the Live2D art-preparation extensions (spec, plan, design, decisions, spike) |
| [f002_headless_backend/](f002_headless_backend/spec.md) | Feature request: drive a headless Krita that `server.py` owns (kickoff + Phase 0 probe; not scheduled) |
| [f003_smoke_test_fixes/](f003_smoke_test_fixes/) | Fixes after the first Live2D smoke test: lossless round trip (device origins), allowlist errors, compact bulk results, quantified diffs, one-call rebuilds, lint name rules |

Feature buildouts get their own `fNNN_<name>/` folder. Anything that
stays true after the buildout ends goes in the top-level docs.
