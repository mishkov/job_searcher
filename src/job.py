from dataclasses import dataclass

@dataclass
class Job:
    title: str
    description: str
    salary: str
    is_remote_allowed: bool | None
    url: str
