
class GoogleSheetException(Exception):
    def __init__(self, log=None, *args: object) -> None:
        super().__init__(*args)
        if log is None:
            log = []
        self.log = log
