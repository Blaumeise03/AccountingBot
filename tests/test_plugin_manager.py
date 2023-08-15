import unittest

from accounting_bot import plugin_manager


class PluginTest(unittest.TestCase):
    def test_get_raw_plugin_config(self):
        cnfg = plugin_manager.get_raw_plugin_config("tests.test_plugin")
        self.assertDictEqual({
            "Name": "Test Plugin",
            "Author": "Blaumeise03",
            "Depends-On": "[test, bot.test]"
        }, cnfg)

    def test_prepare_plugin(self):
        plugin = plugin_manager.prepare_plugin("tests.test_plugin")
        self.assertEqual("Test Plugin", plugin.name)
        self.assertEqual("Blaumeise03", plugin.author)
        self.assertListEqual(["test", "bot.test"], plugin.dependencies)


if __name__ == '__main__':
    unittest.main()
