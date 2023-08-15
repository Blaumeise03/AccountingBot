import logging
import pkgutil
import re
from typing import Dict

from accounting_bot.exceptions import PluginNotFoundException
from accounting_bot.main_bot import PluginWrapper

logger = logging.getLogger("bot.plugins")


def get_raw_plugin_config(plugin_name: str) -> Dict[str, str]:
    """
    Loads the plugin config from a python module. The config has to be at the beginning of the file. All lines have to
    start with '#' or have to be empty. The config part has to start with "PluginConfig" and end with "End". An example
    config looks like this::
        # PluginConfig
        # Name: Name of the plugin
        # Author: Name of the author
        # Depends-On: a, list, of.plugins, that.are, required.for, this.plugin
        # End

    :param plugin_name: The module name of the plugin
    :return: A dictionary with the raw config options
    """
    module = pkgutil.get_loader(plugin_name)
    if module is None:
        raise PluginNotFoundException("Plugin \"" + plugin_name + "\" not found")
    # noinspection PyUnresolvedReferences
    plugin_path = module.get_filename()
    raw_settings = {}
    with open(plugin_path, "r") as file:
        is_config = False
        for line in file:
            if not is_config and not (len(line.lstrip()) == 0 or line.lstrip().startswith("#")):
                break
            if not is_config and "PluginConfig".casefold() in line.casefold():
                is_config = True
            if not is_config:
                continue
            trimmed = re.sub(r"^ *# *", "", line).rstrip("\n")
            if trimmed.casefold().startswith("End".casefold()):
                is_config = False
                break
            if trimmed.startswith("-"):
                continue
            if ":" not in trimmed:
                continue
            split = trimmed.split(":", 1)
            raw_settings[split[0].strip()] = split[1].strip()
    if is_config:
        logger.warning("Module %s has malformed config: Missing End-Tag", plugin_name)
    if len(raw_settings) == 0:
        logger.warning("Module %s has no config", plugin_name)
    return raw_settings


def prepare_plugin(plugin_name: str) -> PluginWrapper:
    """
    Loads the config for a given plugin and returns a PluginWrapper object.

    :param plugin_name: The module name of the plugin
    :return: The Wrapper object
    """
    cnfg = get_raw_plugin_config(plugin_name)
    plugin = PluginWrapper.from_config(plugin_name, cnfg)
    return plugin
