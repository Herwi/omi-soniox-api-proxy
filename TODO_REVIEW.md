# TODO review (as of 2026-04-15)

This document summarizes what remains unimplemented vs. the current proxy plan and behavior.

## Completed in this update

- Keepalive interval is now configurable via `SONIOX_KEEPALIVE_INTERVAL_SECONDS` and clamped to Soniox's real-time keepalive limit (`<=20s`).
- Aggregation now treats both `<end>` and `<fin>` as flush boundaries so manual finalization does not leak control tokens into transcript text.
- `AUDIO_PASSTHROUGH=false` now decodes Omi audio to 16kHz mono PCM with ffmpeg before forwarding to Soniox.

## Remaining TODO items

### 1) Missing protocol-level integration tests for WebSocket bridge behavior

**Current state**
- Unit tests cover only token aggregation.

**Impact**
- Regressions in stream lifecycle handling (disconnects, finalize, Soniox errors, keepalive cadence) can slip through.

**What to implement**
- Add async tests for:
  - Omi `CloseStream` -> Soniox `finalize` + EOF handling.
  - Soniox `finished=true` -> trailing flush and session close.
  - Soniox error payload -> empty segment response and shutdown.
  - Keepalive interval behavior under silence.

### 2) Operational hardening still incomplete

**Current state**
- Retry logic exists for Soniox connect and basic logging is present.
- No metrics, no structured request/session correlation, and no rate limiting.

**Impact**
- Harder production debugging and capacity planning.

**What to implement**
- Add structured logs with per-session IDs.
- Add Prometheus-compatible metrics (connection attempts, failures, segment count, latency).
- Add configurable per-connection and global safeguards (max idle time, max message size).

### 3) Deployment and config docs can be expanded

**Current state**
- README includes local/Docker/deploy basics.

**Impact**
- Teams may miss operational defaults and expected audio format behavior.

**What to implement**
- Add a short compatibility matrix: Omi input format vs. `AUDIO_PASSTHROUGH` setting.
- Add production runbook snippets (healthcheck behavior, restart/backoff strategy, log examples).
