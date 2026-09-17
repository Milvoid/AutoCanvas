"""Configuration belongs to the composition layer, not algorithm modules."""
from dataclasses import dataclass, field, fields
from pathlib import Path
import tomllib


@dataclass
class Settings:
    root: Path = field(default_factory=lambda: Path("runtime").resolve())
    host: str = "127.0.0.1"
    port: int = 8080
    course_ids: list[str] = field(default_factory=list)
    auto_asr: bool = True
    auto_slides: bool = True
    auto_live: bool = True
    course_interval: int = 604800
    sync_interval: int = 3600
    schedule_interval: int = 60
    live_lead_seconds: int = 600
    live_queue_chunks: int = 100
    model: str = "Qwen/Qwen3-ASR-0.6B"
    device: str = "mps"
    chunk_seconds: float = 3
    sample_every: float = 5
    keywords: list[str] = field(default_factory=lambda: ["签到", "点名", "名字"])
    keyword_debounce: int = 30

    @classmethod
    def load(cls, path=None):
        data = {}
        if path:
            path = Path(path)
            data = tomllib.loads(path.read_text())
            if "root" in data:
                data["root"] = (path.resolve().parent / data["root"]).resolve()
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError("Unknown configuration keys: " + ", ".join(sorted(unknown)))
        obj = cls(**data)
        for key in ("course_interval", "sync_interval", "schedule_interval", "live_queue_chunks", "chunk_seconds", "sample_every"):
            if getattr(obj, key) <= 0:
                raise ValueError(key + " must be positive")
        if obj.live_lead_seconds < 0:
            raise ValueError("live_lead_seconds must be nonnegative")
        obj.course_ids = [str(i) for i in obj.course_ids]
        return obj
