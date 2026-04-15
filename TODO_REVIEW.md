# TODO review (as of 2026-04-15)

This document summarizes what remains unimplemented vs. the current proxy plan and behavior.

## Completed in this update

- Keepalive interval is now configurable via `SONIOX_KEEPALIVE_INTERVAL_SECONDS` and clamped to Soniox's real-time keepalive limit (`<=20s`).
- Aggregation now treats both `<end>` and `<fin>` as flush boundaries so manual finalization does not leak control tokens into transcript text.

## Remaining TODO items

### 1) `AUDIO_PASSTHROUGH=false` still does not transcode Omi audio

**Current state**
- The app switches Soniox config to PCM (`pcm_s16le`, 16kHz mono), but forwarded Omi frames are still raw passthrough bytes.

**Impact**
- If Omi sends compressed audio (for example Opus) while passthrough is disabled, Soniox receives mismatched data format.

**What to implement**
- Add a real decode/resample path before forwarding audio in non-passthrough mode.
- Add integration tests with fixture audio to verify packet flow and transcript stability.

### 2) Missing protocol-level integration tests for WebSocket bridge behavior

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

### 3) Operational hardening still incomplete

**Current state**
- Retry logic exists for Soniox connect and basic logging is present.
- No metrics, no structured request/session correlation, and no rate limiting.

**Impact**
- Harder production debugging and capacity planning.

**What to implement**
- Add structured logs with per-session IDs.
- Add Prometheus-compatible metrics (connection attempts, failures, segment count, latency).
- Add configurable per-connection and global safeguards (max idle time, max message size).

### 4) Deployment and config docs can be expanded

**Current state**
- README includes local/Docker/deploy basics.

**Impact**
- Teams may miss operational defaults and expected audio format behavior.

**What to implement**
- Add a short compatibility matrix: Omi input format vs. `AUDIO_PASSTHROUGH` setting.
- Add production runbook snippets (healthcheck behavior, restart/backoff strategy, log examples).
