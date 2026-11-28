"""The Connect IQ beta build: its own app id, only the chosen watches, and a name that says Beta."""

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / "entrypoints" / "garmin-live"
_SPEC = importlib.util.spec_from_file_location("garmin_live_build", ROOT / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)


class BetaBuildTest(unittest.TestCase):
    def test_beta_manifest_swaps_the_app_id_and_lists_only_the_chosen_watches(self):
        text = (ROOT / "manifest.xml").read_text(encoding="utf-8")
        beta = build.beta_manifest(text, ["fr57042mm", "fr57047mm"])
        self.assertIn(f'id="{build.BETA_APP_ID}"', beta)
        self.assertNotIn(build.STORE_APP_ID, beta)
        products = beta[beta.find("<iq:products>"):beta.find("</iq:products>")]
        self.assertEqual(products.count("<iq:product "), 2)
        self.assertIn('id="fr57047mm"', products)
        # Everything else in the manifest -- permissions, languages, API level -- is unchanged.
        self.assertEqual(text[text.find("</iq:products>"):], beta[beta.find("</iq:products>"):])

    def test_the_beta_id_differs_from_the_store_id(self):
        self.assertNotEqual(build.BETA_APP_ID, build.STORE_APP_ID)
        self.assertEqual(len(build.BETA_APP_ID), 32)

    def test_beta_name_says_beta_in_every_language(self):
        for strings in ROOT.glob("resources*/strings/strings.xml"):
            renamed = build.beta_name(strings.read_text(encoding="utf-8"))
            self.assertIn("Long Run Hybrid Coach Beta</string>", renamed)

    def test_a_manifest_without_the_store_id_is_refused(self):
        with self.assertRaises(ValueError):
            build.beta_manifest("<iq:products></iq:products>", ["fr57047mm"])


if __name__ == "__main__":
    unittest.main()
