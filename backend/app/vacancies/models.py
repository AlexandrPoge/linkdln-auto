from dataclasses import dataclass
from datetime import datetime


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
