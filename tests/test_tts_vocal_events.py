"""speech/api.py DialogSession: in direction mode her sound-effect tags that the voice can make
(voice_adapter.VOCAL_EVENTS) ride on the next sentence's TTS text instead of the local-file SFX path,
and the request carries the written direction. DialogSession comes from test_api_carry's AST harness."""
import asyncio
import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock

import sentence_splitter
import test_api_carry as harness
import tts_emotion
import tts_sfx
import voice_adapter as va

DIRECTION = va.default_state()._replace(mode=va.MODE_DIRECTION)
REMAP = va.default_state()


def patched(state):
    adapter = SimpleNamespace(vocal_event=lambda name: va.vocal_event(name, state),
                              adapt_request=lambda payload: va.adapt_request(payload, state),
                              live_state=lambda: state, MODE_DIRECTION=va.MODE_DIRECTION)
    return mock.patch.dict(harness.NS, {
        "voice_adapter": adapter, "parse_sfx_delta": tts_sfx.parse_sfx_delta,
        "StreamingSplitter": sentence_splitter.StreamingSplitter, "prefix_emotion": tts_emotion.prefix_emotion,
    })


def sfx(name):
    return tts_sfx.make_sfx_delta(name)


async def feed(parts):
    s = harness.new_session()
    await s._emit_speech_parts("deep", "t1", parts)
    return s


def items(s):
    return [(i[0] is harness.NS["_SFX_QUEUE"], i[1], i[3] if len(i) > 3 else None) for i in s._tts_sentence_queue]


class VocalEventQueueTests(unittest.TestCase):
    def test_direction_mode_puts_the_event_on_the_next_sentence(self):
        with patched(DIRECTION):
            s = asyncio.run(feed([("casual_happy", "No way."), ("casual_happy", sfx("Laugh")),
                                  ("casual_happy", "Okay, hold on."), ("casual_happy", "\n")]))
        got = items(s)
        self.assertFalse(any(is_sfx for is_sfx, _, _ in got), got)
        self.assertEqual([e for _, text, e in got if text == "No way."], [None])
        after = [(text, e) for _, text, e in got if text != "No way."]
        self.assertEqual(after[0][1], "(laugh)")                     # opens the sentence after the tag
        self.assertTrue(all(e is None for _, e in after[1:]), after)  # once only
        self.assertIsNone(s._pending_vocal_event)

    def test_events_without_a_voice_sound_keep_the_sfx_path(self):
        with patched(DIRECTION):
            s = asyncio.run(feed([("casual_tired", "Long day."), ("casual_tired", sfx("Yawn")),
                                  ("casual_tired", "Bed."), ("casual_tired", "\n")]))
        self.assertIn((True, ("Yawn", "casual_tired"), None), items(s))

    def test_remap_mode_is_unchanged(self):
        with patched(REMAP):
            s = asyncio.run(feed([("casual_happy", "No way."), ("casual_happy", sfx("Laugh")),
                                  ("casual_happy", "Okay."), ("casual_happy", "\n")]))
        got = items(s)
        self.assertIn((True, ("Laugh", "casual_happy"), None), got)
        self.assertTrue(all(e is None for _, _, e in got), got)

    def test_two_tags_in_a_row_both_ride(self):
        with patched(DIRECTION):
            s = asyncio.run(feed([("casual_frustrated", sfx("Throat_clear")), ("casual_frustrated", sfx("Sigh")),
                                  ("casual_frustrated", "Fine."), ("casual_frustrated", "\n")]))
        self.assertEqual(items(s)[0][2], "(clears throat) (sigh)")

    def test_new_turn_drops_a_trailing_event(self):
        with patched(DIRECTION):
            async def main():
                s = await feed([("casual_happy", "Bye."), ("casual_happy", sfx("Laugh"))])
                self.assertEqual(s._pending_vocal_event, "(laugh)")
                s._reset_tts_for_new_turn()
                return s
            s = asyncio.run(main())
        self.assertIsNone(s._pending_vocal_event)


class DirectionRequestTests(unittest.TestCase):
    def test_request_carries_event_text_and_direction_while_display_keeps_the_sentence(self):
        with patched(DIRECTION):
            async def main():
                s = harness.new_session()
                s._committed_turn_id = "t1"
                s.tts._connected = True
                s.voice_output = True
                s._tts_sentence_queue = deque([("casual_frustrated", "Seriously?", "deep", "(sigh)")])
                await s._pump_tts_queue()
                return s
            s = asyncio.run(main())
        sent = [p for e, p in s.tts.sio.emitted if e == "tts_request_gateway"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["text"], "(sigh) Seriously?")
        self.assertEqual(sent[0]["instruction"], va.DEFAULT_DIRECTIONS["casual"].replace("{emotion}", "frustrated"))
        self.assertEqual(sent[0]["cfg_scale"], va.DEFAULT_CFG_SCALE)
        self.assertEqual(s._tts_inflight["sentence"], "Seriously?")
        self.assertEqual(s._tts_inflight["emotion"], "casual_frustrated")

    def test_direction_voice_keeps_its_queue_past_the_v1_first_audio_deadline(self):
        # a whole-sentence voice is slower to first audio; the V1 deadline must not drop the turn's sentences
        async def after_short_deadline(state):
            with patched(state):
                s = harness.new_session()
                s.TTS_FIRST_AUDIO_TIMEOUT_MS, s.TTS_FIRST_AUDIO_TIMEOUT_MS_DIRECTION = 20, 5000
                s._committed_turn_id = "t1"
                s.tts._connected = True
                s.voice_output = True
                s._tts_sentence_queue = deque([("casual_happy", "One.", "deep", None),
                                               ("casual_happy", "Two.", "deep", None)])
                await s._pump_tts_queue()
                await asyncio.sleep(0.1)
                return s._tts_inflight is not None, len(s._tts_sentence_queue)

        self.assertEqual(asyncio.run(after_short_deadline(DIRECTION)), (True, 1))
        self.assertEqual(asyncio.run(after_short_deadline(REMAP)), (False, 0))   # V1 voices: unchanged deadline


if __name__ == "__main__":
    unittest.main()
