import json
import logging
from os.path import exists
from typing import Optional

from accounting_bot.exceptions import ConfigException, ConfigDataTypeException

logger = logging.getLogger("bot.config")


class ConfigTree:
    def __init__(self, raw: Optional[dict] = None, path: Optional[str] = None):
        self.tree = {}
        if path is None:
            self.path = ""
        else:
            self.path = path
        if raw is None:
            return
        for name in raw:
            value = raw[name]
            if type(value) == tuple:
                if len(value) != 2:
                    raise ConfigException(
                        f"Invalid config tree: Expected a tuple of length 1 for {name}, but got {len(tuple)}")
                self.tree[name] = ConfigElement(value[0], value[1])
                continue
            if type(value) == dict:
                sub_tree = ConfigTree(value, f"{self.path}{name}.")
                self.tree[name] = sub_tree

    def __getitem__(self, item):
        if type(item) == str:
            keys = item.split(".")
        elif type(item) == list:
            if len(item) == 0:
                return self
            keys = item
        else:
            return None
        if len(keys) == 0:
            return None
        if keys[0] in self.tree:
            value = self.tree[keys[0]]
            if isinstance(value, ConfigTree):
                return value[keys[1:]]
            elif isinstance(value, ConfigElement):
                return value.value
        return None

    def __setitem__(self, key, value, force=False):
        if type(key) == str:
            keys = key.split(".")
        elif type(key) == list:
            keys = key
        else:
            return False
        if len(keys) == 0:
            return False
        if keys[0] in self.tree:
            val = self.tree[keys[0]]
        else:
            if type(value) == dict:
                val = ConfigTree()
            else:
                val = ConfigElement(None, None)
            self.tree[keys[0]] = val
        if isinstance(val, ConfigTree):
            return val.__setitem__(keys[1:], value, force=force)
        elif isinstance(val, ConfigElement):
            val.value = value
            return True

    def load_from_dict(self, raw_dict: dict) -> bool:
        missing_entry = False
        found = []
        for key in self.tree:
            value = self.tree[key]
            if key in raw_dict:
                raw_value = raw_dict[key]
                if isinstance(value, ConfigTree):
                    if type(raw_value) == dict:
                        value.load_from_dict(raw_value)
                        found.append(key)
                        continue
                    raise ConfigDataTypeException(
                        f"Expected dict, but got {type(raw_value)} for entry {self.path}{key}")
                if isinstance(value, ConfigElement):
                    if type(raw_value) == value.data_type:
                        value.value = raw_value
                        found.append(key)
                        continue
                    raise ConfigDataTypeException(
                        f"Expected {value.data_type}, but got {type(raw_value)} for entry {self.path}{key}")
            else:
                logger.warning("Config entry missing: %s. Using default, please exchange the value.", (self.path + key))
                missing_entry = True
        unknown = list(filter(lambda k: k not in found, raw_dict.keys()))
        if len(unknown) == 0:
            return missing_entry
        for key in unknown:
            value = raw_dict[key]
            if type(value) == dict:
                self.tree[key] = ConfigTree()
                self.tree[key].load_from_dict(value)
            else:
                self.tree[key] = ConfigElement(None, None)
                self.tree[key].value = value

    def to_dict(self):
        res = {}
        for key in self.tree:
            value = self.tree[key]
            if isinstance(value, ConfigTree):
                res[key] = value.to_dict()
            if isinstance(value, ConfigElement):
                res[key] = value.value
        return res

    def __iter__(self):
        return self.tree.__iter__()


class ConfigElement:
    def __init__(self, data_type, default):
        self.value = default
        self.default = default
        self.data_type = data_type

    def __str__(self):
        return str(self.value)


class Config:
    def __init__(self, path: str, tree: ConfigTree, read_only=False):
        self.tree = tree
        self.path = path
        self.read_only = read_only

    def load_config(self):
        if exists(self.path):
            with open(self.path, encoding="utf8") as json_file:
                raw_conf = json.load(json_file)
                self.tree.load_from_dict(raw_conf)
        else:
            logger.warning("Config %s does not exists!", self.path)

    def save_config(self):
        if self.read_only:
            logger.warning("Can't save config %s: Config mode is set to read-only", self.path)
            return
        logger.info("Saving config to %s...", self.path)
        with open(self.path, "w", encoding="utf8") as outfile:
            json.dump(self.tree.to_dict(), outfile, indent=4, ensure_ascii=False)
        logger.info("Config %s saved", self.path)

    def __getitem__(self, item: str):
        return self.tree[item]

    def __setitem__(self, key, value):
        return self.tree.__setitem__(key, value)

