"""speech/voice_adapter.py: the three adapter modes, the config file round trip and its fallbacks."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import voice_adapter as va


class _ConfigDir(unittest.TestCase):
    """Each test gets its own config/voice_map.json and a cold cache."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "voice_map.json"
        self._patch = mock.patch.object(va, "CONFIG_PATH", path)
        self._patch.start()
        va._cache_sig, va._cache_state = None, va.default_state()
        self.path = path

    def tearDown(self):
        self._patch.stop()
        va._cache_sig, va._cache_state = None, va.default_state()
        self._tmp.cleanup()

    def write(self, doc):
        self.path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")


class DirectionModeTests(unittest.TestCase):
    def state(self, **over):
        return va.default_state()._replace(mode=va.MODE_DIRECTION, **over)

    def test_tag_and_anchor_become_a_direction(self):
        payload = {"text": "【serious_very_angry】.You promised me!", "character_id": "main_voice"}
        va.adapt_request(payload, self.state())
        self.assertEqual(payload["text"], "You promised me!")
        self.assertEqual(payload["instruction"], va.DEFAULT_DIRECTIONS["serious"].replace("{emotion}", "very angry"))
        self.assertEqual(payload["cfg_scale"], va.DEFAULT_CFG_SCALE)
        self.assertEqual(payload["character_id"], "main_voice")   # tts_character is the speaker

    def test_only_the_speech_layer_anchor_is_removed(self):
        # api.py sends "." + sentence; an ellipsis the sentence starts with must survive
        payload = {"text": "【casual_shy】....I mean, no.", "character_id": "v"}
        va.adapt_request(payload, self.state())
        self.assertEqual(payload["text"], "...I mean, no.")

    def test_vocal_event_stays_in_the_text(self):
        payload = {"text": "【casual_frustrated】.(sigh) Seriously?", "character_id": "v"}
        va.adapt_request(payload, self.state())
        self.assertEqual(payload["text"], "(sigh) Seriously?")

    def test_key_outside_the_69_speaks_casual_neutral(self):
        for text in ("【happy2】.Hi.", ".Hi.", "Hi."):
            payload = {"text": text, "character_id": "v"}
            va.adapt_request(payload, self.state())
            self.assertEqual(payload["text"], "Hi.", text)
            self.assertEqual(payload["instruction"], va.DEFAULT_DIRECTIONS["casual"].replace("{emotion}", "neutral"))

    def test_operator_direction_without_placeholder_is_sent_verbatim(self):
        directions = va.merge_directions({"whisper": "Barely audible, close to the ear."})
        payload = {"text": "【whisper_caring】.Stay.", "character_id": "v"}
        va.adapt_request(payload, self.state(directions=directions))
        self.assertEqual(payload["instruction"], "Barely audible, close to the ear.")

    def test_vocal_events_only_in_direction_mode(self):
        self.assertEqual(va.vocal_event("Sigh", self.state()), "(sigh)")
        self.assertEqual(va.vocal_event("Can_t_help_laughing", self.state()), "(laugh)")
        self.assertIsNone(va.vocal_event("Yawn", self.state()))     # no event: keeps the local-file path
        self.assertIsNone(va.vocal_event("Sigh", va.default_state()))


class OtherModesTests(unittest.TestCase):
    def test_remap_swaps_tag_and_speaker_without_direction(self):
        payload = {"text": "【casual_happy】.Hi.", "character_id": "main_voice"}
        va.adapt_request(payload, va.default_state())
        self.assertEqual(payload["text"], "【" + va.DEFAULT_MAP["casual_happy"] + "】.Hi.")
        self.assertEqual(payload["character_id"], va.DEFAULT_VOICE)
        self.assertNotIn("instruction", payload)
        self.assertNotIn("cfg_scale", payload)

    def test_off_leaves_the_request_untouched(self):
        payload = {"text": "【casual_happy】.Hi.", "character_id": "mine"}
        va.adapt_request(payload, va.default_state()._replace(mode=va.MODE_OFF))
        self.assertEqual(payload, {"text": "【casual_happy】.Hi.", "character_id": "mine"})


class ConfigFileTests(_ConfigDir):
    def test_missing_file_is_the_shipped_default(self):
        self.assertEqual(va.live_state(), va.default_state())

    def test_file_from_before_modes_keeps_its_meaning(self):
        self.write({"enabled": False, "voice": "X"})
        self.assertEqual(va.live_state().mode, va.MODE_OFF)
        va._cache_sig = None
        self.write({"enabled": True})
        self.assertEqual(va.live_state().mode, va.MODE_REMAP)

    def test_save_round_trip_and_validation(self):
        doc = va.save_state({"mode": "direction", "cfg_scale": "9",
                             "directions": {"casual": "  Bright   and quick. ", "serious": "",
                                            "polite": va.DEFAULT_DIRECTIONS["polite"], "bogus": "x"},
                             "map": {"casual_happy": "playful1", "casual_sad": "nope"}})
        self.assertEqual(doc["cfg_scale"], va.CFG_SCALE_MAX)                 # clamped
        self.assertEqual(doc["directions"], {"casual": "Bright and quick."})  # defaults and junk not stored
        self.assertEqual(doc["map"], {"casual_happy": "playful1"})
        state = va.live_state()
        self.assertEqual(state.mode, va.MODE_DIRECTION)
        self.assertEqual(state.directions["casual"], "Bright and quick.")
        self.assertEqual(state.directions["serious"], va.DEFAULT_DIRECTIONS["serious"])
        self.assertEqual(state.map["casual_happy"], "playful1")

    def test_unusable_values_fall_back(self):
        for raw in ("abc", None, True, float("nan"), float("inf")):
            self.assertEqual(va.pick_cfg_scale(raw), va.DEFAULT_CFG_SCALE, raw)
        self.assertEqual(va.pick_cfg_scale(0.2), va.CFG_SCALE_MIN)
        self.assertEqual(va.pick_mode({"mode": "loud"}), va.DEFAULT_MODE)

    def test_broken_file_keeps_last_good_state(self):
        va.save_state({"mode": "direction", "cfg_scale": 2})
        good = va.live_state()
        self.write("{ not json")
        va._cache_sig = None
        self.assertEqual(va.live_state(), good)

    def test_reset_returns_to_default(self):
        va.save_state({"mode": "off"})
        va.reset_state()
        self.assertFalse(self.path.exists())
        self.assertEqual(va.live_state(), va.default_state())


if __name__ == "__main__":
    unittest.main()
