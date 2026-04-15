import unittest

from aggregator import TokenAggregator


class TokenAggregatorTests(unittest.TestCase):
    def test_flushes_on_end_token(self) -> None:
        agg = TokenAggregator()
        tokens = [
            {"text": "Hello", "start_ms": 0, "end_ms": 400, "is_final": True, "speaker": "1"},
            {"text": " ", "start_ms": 400, "end_ms": 450, "is_final": True, "speaker": "1"},
            {"text": "world", "start_ms": 450, "end_ms": 800, "is_final": True, "speaker": "1"},
            {"text": "<end>", "start_ms": 801, "end_ms": 801, "is_final": True, "speaker": "1"},
        ]

        segments = agg.process_tokens(tokens)

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], "Hello world")
        self.assertEqual(segments[0]["speaker"], "SPEAKER_00")
        self.assertEqual(segments[0]["start"], 0.0)
        self.assertEqual(segments[0]["end"], 0.8)

    def test_speaker_change_forces_flush(self) -> None:
        agg = TokenAggregator()
        tokens = [
            {"text": "Hi", "start_ms": 0, "end_ms": 200, "is_final": True, "speaker": "1"},
            {"text": "there", "start_ms": 210, "end_ms": 450, "is_final": True, "speaker": "1"},
            {"text": "Dzień", "start_ms": 460, "end_ms": 700, "is_final": True, "speaker": "2"},
            {"text": "dobry", "start_ms": 710, "end_ms": 950, "is_final": True, "speaker": "2"},
        ]

        segments = agg.process_tokens(tokens)
        trailing = agg.flush()

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], "Hithere")
        self.assertEqual(segments[0]["speaker"], "SPEAKER_00")
        self.assertEqual(len(trailing), 1)
        self.assertEqual(trailing[0]["speaker"], "SPEAKER_01")
        self.assertEqual(trailing[0]["text"], "Dzieńdobry")

    def test_ignores_non_final_tokens(self) -> None:
        agg = TokenAggregator()
        tokens = [
            {"text": "foo", "start_ms": 0, "end_ms": 200, "is_final": False, "speaker": "1"},
            {"text": "bar", "start_ms": 200, "end_ms": 400, "is_final": True, "speaker": "1"},
            {"text": "<end>", "start_ms": 401, "end_ms": 401, "is_final": True, "speaker": "1"},
        ]

        segments = agg.process_tokens(tokens)

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], "bar")

    def test_flushes_on_fin_token(self) -> None:
        agg = TokenAggregator()
        tokens = [
            {"text": "Good", "start_ms": 0, "end_ms": 200, "is_final": True, "speaker": "1"},
            {"text": "bye", "start_ms": 210, "end_ms": 400, "is_final": True, "speaker": "1"},
            {"text": "<fin>", "start_ms": 401, "end_ms": 401, "is_final": True, "speaker": "1"},
        ]

        segments = agg.process_tokens(tokens)

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], "Goodbye")


if __name__ == "__main__":
    unittest.main()
