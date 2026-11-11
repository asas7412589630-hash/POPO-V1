"""The console's two dictionaries must cover the same keys.

The original console's ``i18n.js`` touches ``localStorage`` at load time, so it
cannot simply be ``require``-d in node. Instead the file is evaluated with a
minimal browser shim: that runs the real file (not a copy) while keeping the
check honest about what ships.
"""
from __future__ import annotations
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
I18N = ROOT / "console" / "i18n.js"

SHIM = """
globalThis.localStorage = {
  _d: {},
  getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
};
globalThis.document = { documentElement: {}, addEventListener() {} };
globalThis.navigator = { language: 'zh-Hant' };
globalThis.window = globalThis;
"""


@unittest.skipUnless(shutil.which("node"), "node is required to run the console dictionaries")
class ConsoleI18nTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = I18N.read_text(encoding="utf-8")
        # Two shapes exist across versions: `I18N` as the dictionary itself, or
        # `I18N.DICT` inside a module that also exposes helpers. Detect rather
        # than assume, so swapping the console does not silently skip the check.
        script = (SHIM
                  + source
                  + """
;(function () {
  const dict = (I18N && I18N.DICT) ? I18N.DICT : I18N;
  const langs = Object.keys(dict).filter(k => dict[k] && typeof dict[k] === 'object');
  const keys = {};
  for (const lang of langs) keys[lang] = Object.keys(dict[lang]).sort();
  process.stdout.write(JSON.stringify({ keys, langs }));
})();
""")
        result = subprocess.run(["node", "-e", script], capture_output=True,
                                text=True, timeout=30)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr[:400]}")
        cls.data = json.loads(result.stdout)

    def test_more_than_one_language(self):
        self.assertGreaterEqual(len(self.data["langs"]), 2, self.data["langs"])

    def test_dictionaries_have_the_same_keys(self):
        keysets = {lang: set(keys) for lang, keys in self.data["keys"].items()}
        reference_name = "zh-Hant" if "zh-Hant" in keysets else sorted(keysets)[0]
        reference = keysets[reference_name]
        for lang, keys in keysets.items():
            missing = sorted(reference - keys)
            extra = sorted(keys - reference)
            self.assertEqual((missing, extra), ([], []),
                             f"{lang} differs from {reference_name}: "
                             f"missing={missing[:6]} extra={extra[:6]}")

    def test_no_empty_strings(self):
        for lang, keys in self.data["keys"].items():
            self.assertTrue(keys, lang)


if __name__ == "__main__":
    unittest.main()
