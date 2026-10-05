from dataclasses import dataclass
from datetime import datetime
import re


SEARCH_TRACKS = ("belarus", "international")


def track_from_location(location: str) -> str:
    """Separate Belarus-only listings from broader international search scopes."""
    parts = re.split(r";|,\s*(?=remote\b)", location, flags=re.IGNORECASE)
    scopes = [match.group(1).strip() for part in parts
              if (match := re.search(r"\bremote\s*[-,]\s*(.+)$", part.strip(), re.IGNORECASE))]
    if len(scopes) == 1 and re.fullmatch(
        r"Belarus(?:\s*\([^)]*\))?", scopes[0], re.IGNORECASE
    ):
        return "belarus"
    return "international"


@dataclass(frozen=True)
class Vacancy:
    source: str
    board_token: str
    external_id: str
    company: str
    title: str
    location: str
    description: str
    url: str
    source_updated_at: datetime | None
    search_track: str = "international"

    def __post_init__(self) -> None:
        if self.search_track not in SEARCH_TRACKS:
            raise ValueError("search_track must be belarus or international")
