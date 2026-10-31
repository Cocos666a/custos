# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Renamed the project from `ctx-rotate` to `custos`: distribution name, CLI
  entry point, Python package, configuration directory (`~/.config/custos`) and
  `CUSTOS_*` environment variables. No compatibility shims are kept.

## [0.1.0] - 2026-10-16

Initial release; not yet published.

### Added

- Usage reading engine with per-tool adapters (Claude Code, Codex, OpenCode, Pi, generic).
- Threshold evaluation with absolute or percent-of-window baselines.
- Handover snapshot writer with a fixed markdown schema.
- Rotation pipeline with interlocks, idempotency ledger and event log.
- Question bank plus answer validators (exact, last number, regex, script).
- Atomic state writes, single-instance file lock and cleaned subprocess environments.
- `custos demo`: the documented end-to-end scenario on synthetic sessions,
  covering both gate selection modes, used as a smoke test.
- Test suite (458 tests) on Python 3.9 and 3.13, plus CI running the matrix, the
  demo and a repository-wide scan for machine-specific strings.
- Bilingual documentation (`README.md`, `README.zh-CN.md`) and per-topic guides
  under `docs/`.

### Notes

- Early development. Interfaces may change before 1.0.
- macOS and Linux only: rotation uses `fcntl` locks and process groups.
