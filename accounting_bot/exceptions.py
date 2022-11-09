from abc import ABC, abstractmethod

from accounting_bot import utils


class LoggedException(ABC, Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)

    @abstractmethod
    def get_log(self) -> str:
        pass


class GoogleSheetException(LoggedException):
    def __init__(self, log=None, progress=None, *args: object) -> None:
        super().__init__(*args)
        if log is None:
            log = []
        self.log = log
        if progress is None:
            progress = []
        self.progress = progress

    def get_log(self) -> str:
        return utils.list_to_string(self.log)


class ConfigException(Exception):
    pass


class ConfigDataTypeException(ConfigException):
    pass
