"""Only deliberate, fixed messages may cross the tool boundary."""


class CalendarError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def invalid(message: str) -> CalendarError:
    return CalendarError("invalid_input", message)
