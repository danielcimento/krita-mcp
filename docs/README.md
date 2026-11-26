# Docs

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | Components, request lifecycle, threading, how to add a command |
| [krita-api-notes.md](krita-api-notes.md) | Krita Python API facts and gotchas, marked verified or to verify |
| [f001_buildout/](f001_buildout/) | Buildout of the Live2D art-preparation extensions (spec, plan, design, decisions, spike) |
| [f002_headless_backend/](f002_headless_backend/spec.md) | Feature request: drive a headless Krita that `server.py` owns (kickoff + Phase 0 probe; not scheduled) |
| [f003_smoke_test_fixes/](f003_smoke_test_fixes/) | Fixes after the first Live2D smoke test: lossless round trip (device origins), allowlist errors, compact bulk results, quantified diffs, one-call rebuilds, lint name rules |
| [f004_live2d_opt_in/](f004_live2d_opt_in/) | Follow-ups after smoke test revision 2 (unbracketed-reference lint, name warnings by kind, render hint) and the survey that makes the Live2D conventions opt-in: core vs. opt-out vs. opt-in behaviour, the `live2d` config section |

Feature buildouts get their own `fNNN_<name>/` folder. Anything that
stays true after the buildout ends goes in the top-level docs.
