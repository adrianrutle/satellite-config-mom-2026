"""The replay recording matches the scenario outcomes, and the page embeds it."""

import json
import unittest

from femm import replay as R


class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = R.trace()
        cls.runs = {s["key"]: s for s in cls.data["scenarios"]}

    def test_outcomes(self):
        expected = {"es1": (342.0, True), "es2": (337.0, True), "es3": (337.0, True),
                    "all": (357.0, True), "chaos": (357.0, True), "reject": (322.0, False)}
        for key, (total, consistent) in expected.items():
            summary = self.runs[key]["summary"]
            self.assertEqual((summary["total"], summary["consistent"], summary["lost"]), (total, consistent, 0), key)

    def test_frames(self):
        types = lambda key: [f.get("type") for f in self.runs[key]["frames"]]
        self.assertIn("conflict.detected", types("es3"))
        self.assertIn("conflict.detected", types("chaos"))
        self.assertIn("command.rejected", types("reject"))
        self.assertFalse(self.runs["all"]["frames"][-1]["state"]["req"]["REQ001"])

    def test_page(self):
        html = R.page(self.data)
        self.assertNotIn("__DATA__", html)
        start = html.index('<script type="application/json" id="data">') + len('<script type="application/json" id="data">')
        embedded = json.loads(html[start:html.index("</script>", start)])
        self.assertEqual([s["key"] for s in embedded["scenarios"]], list(self.runs))


if __name__ == "__main__":
    unittest.main()
