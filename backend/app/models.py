from dataclasses import dataclass
from datetime import datetime
from typing import Literal


Stream = Literal["elec_day", "elec_night", "gas"]


@dataclass(frozen=True)
class Reading:
    ts: datetime
    stream: Stream
    value: float
    source_file: str
