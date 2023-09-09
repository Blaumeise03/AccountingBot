import json
import logging
from os.path import exists
from typing import Dict, Tuple, Any, Type

from accounting_bot.exceptions import ConfigException

logger = logging.getLogger("bot.config")


class ConfigElement:
    def __init__(self, data_type: Type, default: Any):
        self.data_type = data_type
        self.default = default
        self.value = default
        self.unused = False

    def __repr__(self):
        return f"ConfigElement({self.data_type}:{self.value})"


class Config:
    def __init__(self):
        self._tree = {}

    def create_sub_config(self, path: str) -> "Config":
        """
        Creates a new sub config for the given path. All missing keys will be generated.

        :param path: The path to generate
        """
        split = path.split(".", 1)
        key = split[0]
        if key not in self._tree:
            self._tree[key] = Config()
            return self._tree[key]
        if not isinstance(self._tree[key], Config):
            raise ConfigException(f"Can't insert subconfig for key {path}, as this path already has a value")
        if len(split) > 1:
            # noinspection PyProtectedMember
            return self._tree[key].create_sub_config(split[1])
        else:
            return self._tree[key]

    def load_tree(self, tree: Dict[str, Any], root_key: str | None = None, skip_existing=True) -> None:
        """
        Adds a config tree to this config, the tree must be a dictionary with strings as keys. The values must either be
        tuples with the type (str, int, list...) as the first value and a default value for the second value or a
        dictionary for nested configs. Example::
            {
                "keyA": (str, "First default Value"),
                "keyB": (int, 42),
                "keyC": (list, [42, "Hello World"]),
                "keyD": {
                    "subKeyA": (float, 3.5)
                }
            }
        This operation is additive, it's allowed to load multiple config trees. However, duplicated leaves are not
        allowed and will cause a ConfigException.

        :param skip_existing: Ignores if a key already exists in the config, if false raises an error
        :param tree: The config tree to insert
        :param root_key: The key at which the new tree should be inserted, empty for the root key
        :raise ConfigException: If the config tree is malformed
        """
        config = self
        if root_key is not None and len(root_key) > 0:
            self.create_sub_config(root_key)
            config = self[root_key]
        for key, value in tree.items():
            if isinstance(value, Tuple):
                if len(value) != 2:
                    raise ConfigException(f"Expected tuple with length two for key {key}, got length {len(value)}")
                if type(value[0]) != type:
                    raise ConfigException(f"Expected type for first entry of tuple for key {key}, got {type(value[0])}")
                if value[1] is not None and not isinstance(value[1], value[0]):
                    raise ConfigException(f"Expected default value of type {value[0]} for second entry of tuple for key {key}, got {type(value[1])}")
                if key in config._tree:
                    if config._tree[key].unused:
                        config._tree[key].data_type = value[0]
                        config._tree[key].default = value[1]
                        config._tree[key].unused = False
                        continue
                    if skip_existing:
                        continue
                    raise ConfigException(f"Can't load config tree: Key {key} already exists in config")
                config._tree[key] = ConfigElement(value[0], value[1])
            elif isinstance(value, Dict):
                if key in config._tree:
                    if isinstance(config._tree[key], Config):
                        config._tree[key].load_tree(value)
                        continue
                    raise ConfigException(f"Can't load config tree: Key {key} already exists in config but is not a subconfig")
                sub_config = Config()
                sub_config.load_tree(value)
                config._tree[key] = sub_config
            else:
                raise ConfigException(f"Unexpected value for key {key}: {type(value)}")

    def _to_dict(self) -> Dict[str, Any]:
        """
        Returns the config as a dictionary. Used for converting the config to JSON to save it.

        :return: The config as a dictionary
        """
        result = {}
        for key, value in self._tree.items():
            if isinstance(value, ConfigElement):
                result[key] = value.value
            elif isinstance(value, Config):
                result[key] = value._to_dict()
            else:
                raise ConfigException(f"Unexpected value for key {key}: {type(value)}")
        return result

    def _from_dict(self, raw: Dict[str, Any]) -> None:
        """
        Loads the values of a dictionary into the config.

        :param raw: The raw config as a dictionary to load from
        """
        for key, value in raw.items():
            if key not in self:
                split = key.split(".", 1)
                if len(split) > 1:
                    self.create_sub_config(split[0])
                if isinstance(value, Dict):
                    self.create_sub_config(split[0])
                    self[split[0]]._from_dict(value)
                else:
                    self._tree[key] = ConfigElement(type(value), value)
                if isinstance(self._tree[key], ConfigElement):
                    self._tree[key].unused = True
            else:
                self[key] = value

    def load_config(self, path: str):
        """
        Loads the config from a file. Existing data will be updated, keys that exist only in the file but not the
        current config will still be added to the config.

        :param path: The path of the file
        """
        if exists(path):
            with open(path, encoding="utf8") as json_file:
                raw_conf = json.load(json_file)
                self._from_dict(raw_conf)
        else:
            logger.warning("Config %s does not exists!", path)

    def save_config(self, path: str):
        """
        Saves the config to the file system.

        :param path: The path of the file
        """
        raw = self._to_dict()
        with open(path, "w", encoding="utf8") as outfile:
            json.dump(raw, outfile, indent=4, ensure_ascii=False)
        logger.info("Config %s saved", path)

    def __getitem__(self, key: str):
        split = key.split(".", 1)
        value = self._tree[split[0]]
        if isinstance(value, ConfigElement):
            return value.value
        elif isinstance(value, Config):
            if len(split) > 1:
                return value[split[1]]
            return value
        raise ConfigException(f"Unexpected value for key {key}: {type(value)}. Expected ConfigElement or Config")

    def __setitem__(self, key, value):
        split = key.split(".", 1)
        element = self._tree[split[0]]
        if isinstance(element, ConfigElement):
            element.value = value
            return
        elif isinstance(element, Config):
            if len(split) > 1:
                element[split[1]] = value
                return
            if isinstance(value, Dict):
                for k, v in value.items():
                    self[split[0]][k] = v
                return
            raise ConfigException(f"Can't update value for key {key} as it is a subconfig")
        raise ConfigException(f"Unexpected value for key {key}: {type(element)}. Expected ConfigElement or Config")

    def __contains__(self, key):
        try:
            _ = self.__getitem__(key)
            return True
        except (ConfigException, KeyError):
            return False

    def __iter__(self):
        return self._tree.__iter__()
