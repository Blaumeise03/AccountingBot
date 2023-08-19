# Plugins
This document describes how to create own plugins and how to use the localization feature.
This is NOT a tutorial on how to install plugins.

<!-- TOC -->
* [Plugins](#plugins)
  * [Basics](#basics)
  * [Lifecycle Hooks](#lifecycle-hooks)
  * [Config](#config)
  * [Localization](#localization)
  * [Example](#example)
<!-- TOC -->

## Basics
Every plugin consists of a main python file. The plugin config is at the beginning of the file
and configures how the plugin should be loaded. Also, this file must contain a Plugin class that
inherits from the class [accounting_bot.main_bot.BotPlugin](../accounting_bot/main_bot.py). This
class provides the lifecycle hooks which will be the entry point for your plugin. Using those, you
can register events inside those.

The constructor of the plugin takes two parameters, the bot and the `PluginWrapper`. Those two will
be supplied by the bot itself when the plugin gets loaded. It is required to call the super constructor,
or the plugin will not work. Example for a minimal plugin:
```python
import logging
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper
logger = logging.getLogger("test.plugin_test")


class MyPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
```
Note: the logger is optional if no logger is given, a new logger with the module name will be created.

To add the plugin to your bot, add the full module name to the config of the bot:
```json
{
    "plugins": [
        "tests.plugin_test",
        "accounting_bot.ext.help_command"
    ]
}
```
The module has to be at a location where it can be found by python.

## Lifecycle Hooks
Every plugin has a state. The main states are `UNLOADED, LOADED, ENABLED`, this is also the order
in which the states change. Loading happens before the bot logs in to discord. The corresponding
hook is `on_load`. This function gets called when the plugin is loaded. It may not raise an error or
the plugin will get marked as `CRASHED` (same applies to the other lifecycle hooks). Inside this
lifecycle hook, the plugin may register Cogs for commands and event listener. All Cogs will
automatically be removed when the plugin gets disabled. The loading order of plugins depends on their
dependencies, which can be defined in the config.

After all plugins are loaded and the bot has logged in to discord, they will get enabled. The
hook `on_enable` is an async function and discord API calls may be executed. However, this will
slow down the startup time of other plugins as the plugins will get enabled after each other (same)
order as they have loaded.

When the bot gets shut down, the plugins will first be disabled before unloading. Disabling will
happen while the bot is still online and discord API access is still possible. Unloading will happen
after the bot has disconnected from the discord API.

Disabling and unloading are not reliable as the bot might be forcefully terminated before all plugins
have been unloaded. Therefore, these lifecycle hooks (`on_disable` and `on_unload`) should not be
used to save important data.

## Config
The plugin config has to be a python comment at the beginning of the file. It is allowed to have other
comments before the config itself, however only line comments (starting with `#`) are allowed. The config
itself has this format (case-sensitive):
```python
# PluginConfig
# Name: TestPlugin
# Author: Blaumeise03
# Depends-On: []
# Localization: plugin_test_lang.xml
# End

import logging
...
```
The name may not contain spaces, while the author can contain spaces. After the `Depends-On` section it's
possible to specify a list of plugins (note: The name of the plugin modules has to be specified, not the names)
that have to be loaded before this one. If one of these plugins could not be found or loaded, this plugin
will also not be loaded. Example:
```
# Depends-On: [test.other_test_plugin, accounting_bot.ext.help_command]
```
With the localization option, it is possible to specify the localization file (only XML format) relative
to the location of the main plugin file. It is recommended to put them in the same directory as the plugin.

## Localization
Plugins can make use of the automatic localization system which gets the locale of users during slash 
commands from discord. Also, the automatic `/help` command uses the localization system to provide
more detailed descriptions for the commands. The help command searches for a description of the
command itself and for its options. For the command itself, the lookup order is as follows (with
`cmd_name` beging the name of the command, for subcommands replace the spaces with underlines `_`):
1. `help_{cmd_name}_long` (only for the long description)
2. `help_{cmd_name}`
3. The description specified when registering the command

For the options (with `opt` being the name of the option), the order is as follows:
1. `help_{cmd_name}_{opt}_long` (only for the long description)
2. `help_{cmd_name}_{opt}` 
3. `help_{opt}_long` (only for the long description
4. `help_{opt}`

As the same command options can get used for multiple commands, it is possible to save the translations
one time for all commands instead of every single command (it is still possible to overwrite the global
translation).

## Example
For a full example, please refer to the test plugin inside the test package: [tests.plugin_test](../tests/plugin_test.py)
with the example localization file [plugin_test_lang.xml](../tests/plugin_test_lang.xml)
