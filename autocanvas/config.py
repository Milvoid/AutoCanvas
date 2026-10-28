"""Validated application settings. Saved UI overrides also apply to the CLI."""
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
import json
import math
import tomllib


@dataclass
class Settings:
    root: Path = field(default_factory=lambda: Path('runtime').resolve())
    host: str = '127.0.0.1'
    port: int = 8080
    course_ids: list[str] = field(default_factory=list)
    auto_asr: bool = True
    auto_slides: bool = True
    auto_live: bool = True
    course_interval: int = 604800
    sync_interval: int = 3600
    assignment_interval: int = 1800
    schedule_interval: int = 60
    live_lead_seconds: int = 600
    live_queue_chunks: int = 100
    model: str = 'Qwen/Qwen3-ASR-0.6B'
    device: str = 'mps'
    asr_backend: str = 'torch'
    asr_dtype: str = 'auto'
    asr_idle_seconds: int = 120
    chunk_seconds: float = 3
    sample_every: float = 5
    keywords: list[str] = field(default_factory=lambda: ['签到', '点名', '名字'])
    keyword_debounce: int = 30

    def validate(self):
        for key in ('auto_asr', 'auto_slides', 'auto_live'):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f'{key}: 必须为布尔值')
        for key in ('port', 'course_interval', 'sync_interval', 'assignment_interval', 'schedule_interval', 'live_lead_seconds', 'live_queue_chunks', 'keyword_debounce', 'asr_idle_seconds'):
            value = getattr(self, key)
            minimum = 0 if key in ('live_lead_seconds', 'keyword_debounce') else 1
            if type(value) is not int or not minimum <= value <= (65535 if key == 'port' else 31536000):
                raise ValueError(f'{key}: 整数超出允许范围')
        if self.asr_backend not in ('torch', 'mlx'):
            raise ValueError('asr_backend: 必须为 torch/mlx')
        if self.asr_dtype not in ('auto', 'float32', 'float16', 'bfloat16'):
            raise ValueError('asr_dtype: 必须为 auto/float32/float16/bfloat16')
        if self.live_queue_chunks > 10000:
            raise ValueError('live_queue_chunks: 最大为 10000')
        for key in ('chunk_seconds', 'sample_every'):
            value = getattr(self, key)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0.1 <= value <= 3600:
                raise ValueError(f'{key}: 必须在 0.1 到 3600 秒之间')
        for key in ('host', 'model', 'device'):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip() or len(value) > 1024 or any(c in value for c in '\r\n\x00'):
                raise ValueError(f'{key}: 无效文本')
        for key in ('course_ids', 'keywords'):
            value = getattr(self, key)
            if not isinstance(value, list) or len(value) > 1000 or any(not isinstance(v, str) or not v.strip() for v in value):
                raise ValueError(f'{key}: 必须为非空字符串列表')
        if any(not v.isdigit() for v in self.course_ids):
            raise ValueError('course_ids: 课程编号必须是数字')
        if not isinstance(self.root, (str, Path)) or not str(self.root).strip():
            raise ValueError('root: 需要有效路径')
        self.root = Path(self.root).expanduser().resolve()
        return self

    def public(self):
        return {**asdict(self), 'root': str(self.root)}

    @classmethod
    def load(cls, path=None, root=None):
        data = {}
        if path:
            path = Path(path)
            data = tomllib.loads(path.read_text())
            if 'root' in data:
                data['root'] = (path.resolve().parent / data['root']).resolve()
        if root is not None:
            data['root'] = Path(root).resolve()
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError('Unknown configuration keys: ' + ', '.join(sorted(unknown)))
        settings = cls(**data).validate()
        saved_path = settings.root/'settings.json'
        if saved_path.exists():
            saved = json.loads(saved_path.read_text())
            settings = cls(**{**settings.public(), **saved}).validate()
        settings._settings_path = saved_path
        return settings
