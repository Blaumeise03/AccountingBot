import unittest

from accounting_bot import main_bot
from accounting_bot.exceptions import PluginDependencyException
from accounting_bot.main_bot import PluginWrapper, AccountingBot, PluginState


class PluginTest(unittest.TestCase):
    def test_get_raw_plugin_config(self):
        cnfg = main_bot.get_raw_plugin_config("tests.plugin_test")
        self.assertDictEqual({
            "Name": "TestPlugin",
            "Author": "Blaumeise03",
            "Depends-On": "[]",
            "Localization": "plugin_test_lang.xml",
            "Load-After": "[]"
        }, cnfg)

    def test_prepare_plugin(self):
        plugin = main_bot.prepare_plugin("tests.plugin_test")
        self.assertEqual("TestPlugin", plugin.name)
        self.assertEqual("Blaumeise03", plugin.author)
        self.assertListEqual([], plugin.dep_names)

    def test_load_plugin(self):
        bot = AccountingBot(config_path=None)
        plugin = main_bot.prepare_plugin("tests.plugin_test")
        plugin.load_plugin(bot)
        self.assertEqual(PluginState.LOADED, plugin.state)

    def test_plugin_order(self):
        plugins = [
            PluginWrapper(name="A", module_name="A", dep_names=["C", "F"]),
            PluginWrapper(name="C", module_name="C", dep_names=["D", "B"]),
            PluginWrapper(name="B", module_name="B", opt_dep_names=["D", "E"]),
            PluginWrapper(name="D", module_name="D", dep_names=["E"], opt_dep_names=["F"]),
            PluginWrapper(name="E", module_name="E"),
            PluginWrapper(name="F", module_name="F", dep_names=["E"])
        ]
        res = main_bot.find_plugin_order(plugins)
        self.assertEqual("EFDBCA", "".join(p.module_name for p in res))

        # Add cyclic dependency requirement
        plugins[4].dep_names.append("A")
        self.assertRaises(PluginDependencyException, main_bot.find_plugin_order, plugins)


if __name__ == '__main__':
    unittest.main()
