from dataclasses import dataclass


@dataclass(slots=True)
class EDSAccountCredentials:
    login: str
    password: str
