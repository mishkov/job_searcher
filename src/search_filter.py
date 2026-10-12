from dataclasses import dataclass, field


@dataclass
class SearchFilter:
    """Represents filters that may be used to filter vacancies during search.

    Default constructor without parameters results in "no filters".

    exclude_keywords takes priority over keywords, it means that first we check if vacancy contain any exclude_keywords then we check keywords.

    Attributes:
        keywords: the result will contain the vacancy only if it contains any of the keywords.
        exclude_keywords: the result won't contain the vacancy if it contains any of these keywords.
    """

    keywords: list[str] = field(
        default_factory=lambda: ["*"]
    )
    exclude_keywords: list[str] = field(default_factory=list)