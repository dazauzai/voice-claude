import unittest

from voiceclaude import voices


class VoiceNameTests(unittest.TestCase):
    def test_names_and_numbers(self):
        self.assertEqual(voices.resolve("小山"), 60)
        self.assertEqual(voices.resolve("换成小山。"), 60)
        self.assertEqual(voices.resolve("60 号"), 60)
        self.assertEqual(voices.resolve("7"), 7)
        self.assertIsNone(voices.resolve("大象"))
        self.assertIsNone(voices.resolve("500"))
        self.assertEqual(voices.name_of(3), "小雨")
        self.assertEqual(voices.name_of(7), "7 号")
        self.assertIn("小山（60 号）", voices.menu())


if __name__ == "__main__":
    unittest.main()
