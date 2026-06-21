# Coverage badge handoff

Generated from the local test suite (1209 passing). Measured with:
`python3 -m coverage run --source=<first-party packages> -m pytest tests/`

## Numbers

| Scope | Coverage |
|-------|----------|
| **Project-wide (all first-party source)** | **74%** (20058 stmts, 5179 missed) |
| UI package (`ui/`) | 86% |
| Core Auto-UV / profile logic (`auto_uv`, `profiles.uv`) | ~100% |

## Ready-to-paste badge (shields static)

Project-wide:

```md
![coverage](https://img.shields.io/badge/coverage-74%25-yellowgreen)
```

UI-specific (if a component badge is wanted):

```md
![ui coverage](https://img.shields.io/badge/ui%20coverage-86%25-brightgreen)
```

Note: these are static badges (no CI coverage service is wired up). If you want
a live badge, add a CI job that uploads to Codecov/Coveralls and use their badge
URL instead.

> This number will tick up slightly after the in-progress UI editor refactor
> (it removes more uncovered lines than covered ones). Re-measure before final
> publish if timing allows.
