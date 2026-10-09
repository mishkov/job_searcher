from dataclasses import dataclass


@dataclass
class SearchFilter:
    keywords: list[str]