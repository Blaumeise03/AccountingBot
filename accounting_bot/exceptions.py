from abc import ABC, abstractmethod

from discord.ext.commands import CommandError, CheckFailure


class UnexpectedStateException(Exception):
    pass


class InputException(CommandError):
    pass


class AccountingException(InputException):
    pass


class LoggedException(ABC, Exception):
    """
    An abstract exception that contains an error log that may be made public to the end user.
    """

    def __init__(self, *args: object) -> None:
        super().__init__(*args)

    @abstractmethod
    def get_log(self) -> str:
        pass


def list_to_string(line: [str]):
    res = ""
    for s in line:
        res += s + "\n"
    return res


class GoogleSheetException(LoggedException):
    """
    An exception that got caused during the interaction with a Google Sheet, containing a dedicated log with more
    details on what happened and the progress of.
    """

    def __init__(self, log=None, *args: object, progress=None, ) -> None:
        super().__init__(*args)
        if log is None:
            log = []
        self.log = log
        if progress is None:
            progress = []
        self.progress = progress

    def get_log(self) -> str:
        return list_to_string(self.log)


class ConfigException(Exception):
    pass


class ConfigDataTypeException(ConfigException):
    pass


class BotOfflineException(Exception):
    def __init__(self, message="Action can't be executed", *args: object) -> None:
        super().__init__(str(message), *args)


class PlanetaryProductionException(InputException):
    pass


class PiPlanerException(Exception):
    pass


class DatabaseException(Exception):
    pass


class KillmailException(Exception):
    pass


class NoPermissionException(InputException):
    pass


class SingletonException(Exception):
    pass


class PluginException(Exception):
    pass


class PluginLoadException(PluginException):
    pass


class PluginDependencyException(PluginLoadException):
    pass


class PluginNotFoundException(PluginLoadException):
    pass


class UnhandledCheckException(CheckFailure):
    pass


class UsernameNotFoundException(Exception):
    pass
