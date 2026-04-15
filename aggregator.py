from dataclasses import dataclass
from typing import Any


@dataclass
class BufferedSegment:
    text_parts: list[str]
    start_ms: float
    end_ms: float
    speaker: str


class TokenAggregator:
    """Aggregates Soniox final tokens into Omi-compatible segments."""

    def __init__(self) -> None:
        self._buffer: BufferedSegment | None = None
        self._speaker_map: dict[str, str] = {}

    def _map_speaker(self, soniox_speaker: str | None) -> str | None:
        if soniox_speaker is None:
            return None
        if soniox_speaker not in self._speaker_map:
            mapped = f"SPEAKER_{len(self._speaker_map):02d}"
            self._speaker_map[soniox_speaker] = mapped
        return self._speaker_map[soniox_speaker]

    def _flush_current(self) -> dict[str, Any] | None:
        if self._buffer is None:
            return None

        text = "".join(self._buffer.text_parts).strip()
        if not text:
            self._buffer = None
            return None

        segment: dict[str, Any] = {
            "text": text,
            "start": round(self._buffer.start_ms / 1000.0, 3),
            "end": round(self._buffer.end_ms / 1000.0, 3),
        }
        mapped_speaker = self._map_speaker(self._buffer.speaker)
        if mapped_speaker is not None:
            segment["speaker"] = mapped_speaker

        self._buffer = None
        return segment

    def process_tokens(self, tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Process a Soniox token batch and emit finalized segments."""
        segments: list[dict[str, Any]] = []
        boundary_tokens = {"<end>", "<fin>"}

        for token in tokens:
            if token.get("is_final") is not True:
                continue

            text = token.get("text", "")
            speaker = token.get("speaker")
            start_ms = float(token.get("start_ms", 0.0))
            end_ms = float(token.get("end_ms", start_ms))

            if text in boundary_tokens:
                flushed = self._flush_current()
                if flushed is not None:
                    segments.append(flushed)
                continue

            if self._buffer is None:
                self._buffer = BufferedSegment(
                    text_parts=[text],
                    start_ms=start_ms,
                    end_ms=end_ms,
                    speaker=speaker,
                )
                continue

            speaker_changed = self._buffer.speaker != speaker
            if speaker_changed:
                flushed = self._flush_current()
                if flushed is not None:
                    segments.append(flushed)
                self._buffer = BufferedSegment(
                    text_parts=[text],
                    start_ms=start_ms,
                    end_ms=end_ms,
                    speaker=speaker,
                )
            else:
                self._buffer.text_parts.append(text)
                self._buffer.end_ms = end_ms

        return segments

    def flush(self) -> list[dict[str, Any]]:
        flushed = self._flush_current()
        return [flushed] if flushed is not None else []
