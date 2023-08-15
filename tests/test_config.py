import os
import unittest

from accounting_bot.config import Config

CFG_PATH = "tmp_test_config.json"


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        if os.path.exists(CFG_PATH):
            os.remove(CFG_PATH)
        super().setUp()

    def tearDown(self) -> None:
        if os.path.exists(CFG_PATH):
            os.remove(CFG_PATH)
        super().tearDown()

    def test_creating(self):
        config = Config()
        config.load_tree({
            "keyA": (str, "DefA"),
            "keyB": (list, ["DefB"]),
            "keyC": {
                "keyC2": (int, 42)
            }
        })
        self.assertEqual("DefA", config["keyA"])
        self.assertListEqual(["DefB"], config["keyB"])
        self.assertEqual(42, config["keyC.keyC2"])
        config.load_tree({
            "keyF": (str, "DefF"),
            "keyG": (float, 0.5)
        }, "keyC.keyC3.keyC4")
        self.assertEqual("DefF", config["keyC.keyC3.keyC4.keyF"])
        self.assertEqual(0.5, config["keyC.keyC3.keyC4.keyG"])

    def test_save_load(self):
        config_a = Config()
        config_a.load_tree({
            "keyA": (str, "DefA"),
            "keyB": (list, ["DefB"]),
            "keyC": {
                "keyC2": (int, 42)
            }
        })
        config_a["keyC.keyC2"] = 40
        config_a["keyB"].append("DefBB")
        config_a.save_config(CFG_PATH)
        config_b = Config()
        config_b.load_tree({
            "keyA": (str, "DefA2"),
            "keyC": {
                "keyC2": (int, 41),
                "keyC3": (float, 0.5)
            }
        })
        config_b.load_config(CFG_PATH)
        self.assertEqual("DefA", config_b["keyA"])
        self.assertEqual(40, config_b["keyC.keyC2"])
        self.assertEqual(0.5, config_b["keyC.keyC3"])
        self.assertListEqual(["DefB", "DefBB"], config_b["keyB"])


if __name__ == '__main__':
    unittest.main()
