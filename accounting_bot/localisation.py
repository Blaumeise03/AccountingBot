import contextvars
import csv
import logging
from os import PathLike
from typing import Callable, Dict, Union, Optional

import xmltodict as xmltodict
from discord.ext.commands import Bot, Context

logger = logging.getLogger("bot.localisation")


class LocalisationHandler(object):
    default_handler = None  # type: LocalisationHandler | None

    def __init__(self) -> None:
        self.languages = {}  # type: Dict[str, Language]
        self._current_locale = contextvars.ContextVar("_current_locale")
        self.fallback = "en"
        LocalisationHandler.default_handler = self

    def init_bot(self, bot: Bot, get_locale: Callable[[Context], str]):
        async def pre_hook_localization(ctx: Context):
            self.set_current_locale(get_locale(ctx))
        bot.before_invoke(pre_hook_localization)

    def _add_translation(self, key: str, lan: str, value: str):
        if lan not in self.languages:
            language = Language(lan)
            self.languages[lan] = language
        self.languages[lan][key] = value

    def load_from_csv(self, path: Union[PathLike, str]):
        with open(path, mode="r", encoding="utf8") as csv_file:
            reader = csv.DictReader(csv_file)
            for line in reader:
                for lan, val in line.items():
                    if lan == "key":
                        continue
                    self._add_translation(line["key"], lan, val)

    def load_from_xml(self, path: Union[PathLike, str]):
        logger.info("Loading localisation data")
        with open(path, mode="rb") as file:
            lang_dict = xmltodict.parse(file, encoding="UTF-8")
        for key, translations in lang_dict["translations"].items():
            for lang, value in translations.items():
                if type(value) != str:
                    raise LocalizationException(f"Value for {lang}:{key} is not a string, got {type(value)}:{value}")
                self._add_translation(key, lang, value)
        logger.info("Localisation data loaded")

    def set_current_locale(self, locale: str):
        self._current_locale.set(locale)

    def get_current_locale(self) -> str:
        lang = self._current_locale.get(self.fallback)
        return lang or self.fallback

    def get_text(self, key: str, raise_not_found=True) -> str:
        language = self.get_current_locale()
        if language not in self.languages:
            raise LanguageNotFoundException(f"Language '{language}' not found")
        try:
            return self.languages[language].get_translation(key)
        except TranslationNotFound:
            return self.languages[self.fallback].get_translation(key, raise_not_found)

    @classmethod
    def get_translation(cls, key: str, raise_not_found=True):
        return cls.default_handler.get_text(key, raise_not_found)


t_ = LocalisationHandler.get_translation


class Language(object):
    def __init__(self, name: str):
        self.name = name
        self.translations = {}  # type: Dict[str, str]

    def get_translation(self, key: str, raise_not_found=True) -> Optional[str]:
        if key in self.translations:
            return self.translations[key]
        if raise_not_found:
            raise TranslationNotFound(f"Key '{key}' not found for '{self.name}'")
        return None

    def __getitem__(self, item) -> str:
        return self.get_translation(item)

    def __setitem__(self, item, value):
        self.translations[item] = value

    def load_from_dict(self, data: Dict):
        for k, v in data.items():
            self.translations[k] = v


class LocalizationException(Exception):
    pass


class LanguageNotFoundException(Exception):
    pass


class TranslationNotFound(Exception):
    pass
