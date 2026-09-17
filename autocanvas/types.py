"""Small immutable values shared at module boundaries; no services or state."""
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def timestamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, SHANGHAI).isoformat()
    if not value:
        return None
    dt = datetime.fromisoformat(str(value))
    return (dt if dt.tzinfo else dt.replace(tzinfo=SHANGHAI)).isoformat()


@dataclass(frozen=True)
class MediaSource:
    location: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    view: str = "unknown"


@dataclass(frozen=True)
class AudioChunk:
    pcm: bytes = field(repr=False)  # signed 16-bit, little-endian, mono
    sample_rate: int
    start: float

    @property
    def duration(self):
        return len(self.pcm) / (2 * self.sample_rate)


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


class AuthenticationRequired(RuntimeError):
    pass


class RemoteError(RuntimeError):
    pass


class MediaError(RuntimeError):
    pass


class VideoUnavailable(RemoteError):
    """Course has no teaching-class mapping on the new platform."""
    pass
