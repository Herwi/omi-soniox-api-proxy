# TODO review (as of 2026-04-15)

This document summarizes what remains unimplemented vs. the current proxy plan and behavior.

## Completed in this update

- Keepalive interval is now configurable via `SONIOX_KEEPALIVE_INTERVAL_SECONDS` and clamped to Soniox's real-time keepalive limit (`<=20s`).
- Aggregation now treats both `<end>` and `<fin>` as flush boundaries so manual finalization does not leak control tokens into transcript text.
- `AUDIO_PASSTHROUGH=false` now decodes Omi audio to 16kHz mono PCM with ffmpeg before forwarding to Soniox.
- Added protocol-level async bridge tests for `CloseStream` finalize/EOF, `finished=true` trailing flush, Soniox error shutdown payload, and keepalive behavior under silence.

## Remaining TODO items

### 1) Operational hardening still incomplete

**Current state**
- Retry logic exists for Soniox connect and basic logging is present.
- No metrics, no structured request/session correlation, and no rate limiting.

**Impact**
- Harder production debugging and capacity planning.

**What to implement**
- Add structured logs with per-session IDs.
- Add Prometheus-compatible metrics (connection attempts, failures, segment count, latency).
- Add configurable per-connection and global safeguards (max idle time, max message size).

### 2) Deployment and config docs can be expanded

**Current state**
- README includes local/Docker/deploy basics.

**Impact**
- Teams may miss operational defaults and expected audio format behavior.

**What to implement**
- Add a short compatibility matrix: Omi input format vs. `AUDIO_PASSTHROUGH` setting.
- Add production runbook snippets (healthcheck behavior, restart/backoff strategy, log examples).
