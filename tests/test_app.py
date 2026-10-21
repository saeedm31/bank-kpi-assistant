"""Run the Streamlit app headless and check the main paths work (needs streamlit)."""

import os
import unittest

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # streamlit not installed: skip
    AppTest = None

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app.py")


@unittest.skipIf(AppTest is None, "streamlit not installed")
class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.at = AppTest.from_file(APP, default_timeout=300).run()

    def test_page_renders(self):
        at = self.at
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertIn("Bank KPI Assistant", at.title[0].value)
        self.assertGreaterEqual(len(at.metric), 5)        # KPI tiles (+ accuracy numbers)

    def test_chat_answers_and_follow_up(self):
        at = self.at
        at.chat_input[0].set_value("Card spend by branch last month").run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        text = " ".join(m.value for m in at.markdown)
        self.assertIn("has the highest card spend", text)
        at.chat_input[0].set_value("and for premium customers?").run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertIn("card spend for Premium", " ".join(m.value for m in at.markdown))

    def test_saved_example_button(self):
        at = self.at
        button = next(b for b in at.button if b.label.startswith("What share of mortgage customers"))
        button.click().run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertIn("savings account", " ".join(m.value for m in at.markdown))


if __name__ == "__main__":
    unittest.main()
