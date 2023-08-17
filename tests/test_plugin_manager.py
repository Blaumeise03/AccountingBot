import asyncio
import unittest

from accounting_bot import main_bot, config
from accounting_bot.exceptions import PluginDependencyException
from accounting_bot.main_bot import PluginWrapper, AccountingBot, PluginStatus


class PluginTest(unittest.TestCase):
    def test_get_raw_plugin_config(self):
        cnfg = main_bot.get_raw_plugin_config("tests.plugin_test")
        self.assertDictEqual({
            "Name": "Test Plugin",
            "Author": "Blaumeise03",
            "Depends-On": "[]"
        }, cnfg)

    def test_prepare_plugin(self):
        plugin = main_bot.prepare_plugin("tests.plugin_test")
        self.assertEqual("Test Plugin", plugin.name)
        self.assertEqual("Blaumeise03", plugin.author)
        self.assertListEqual([], plugin.dep_names)

    def test_load_plugin(self):
        plugin = main_bot.prepare_plugin("tests.plugin_test")
        plugin.load_plugin(None)
        self.assertTrue(True)

    def test_plugin_order(self):
        plugins = [
            PluginWrapper(name="A", module_name="A", dep_names=["C", "F"]),
            PluginWrapper(name="C", module_name="C", dep_names=["D", "B"]),
            PluginWrapper(name="B", module_name="B", dep_names=["D", "E"]),
            PluginWrapper(name="D", module_name="D", dep_names=["E", "F"]),
            PluginWrapper(name="E", module_name="E"),
            PluginWrapper(name="F", module_name="F", dep_names=["E"])
        ]
        res = main_bot.find_plugin_order(plugins)
        self.assertEqual("EFDBCA", "".join(p.module_name for p in res))

        # Add cyclic dependency requirement
        plugins[4].dep_names.append("A")
        self.assertRaises(PluginDependencyException, main_bot.find_plugin_order, plugins)

    def test_plugin_lifecycle(self):
        bot = AccountingBot()
        cnfg = config.Config()
        cnfg.load_tree({
            "plugins": (list, ["tests.plugin_test"])
        })
        bot.config = cnfg
        bot.load_plugins()
        self.assertEqual(1, len(bot.plugins))
        self.assertEqual(PluginStatus.LOADED, bot.plugins[0].status)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(bot.enable_plugins())
        self.assertEqual(1, len(bot.plugins))
        self.assertEqual(PluginStatus.ENABLED, bot.plugins[0].status)
        loop.run_until_complete(bot.plugins[0].reload_plugin(bot))
        self.assertEqual(1, len(bot.plugins))
        self.assertEqual(PluginStatus.ENABLED, bot.plugins[0].status)
        loop.run_until_complete(bot.shutdown())
        self.assertEqual(PluginStatus.UNLOADED, bot.plugins[0].status)


if __name__ == '__main__':
    unittest.main()
