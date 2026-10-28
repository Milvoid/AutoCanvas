"""Local MLX inference with explicit session ownership and offline model loading."""
import json
from pathlib import Path
from .types import TranscriptSegment


class Recognizer:
    def __init__(self, model, device='mps', language='Chinese', silence=0.005, dtype='auto'):
        self.model_name, self.language, self.silence = model, language, silence
        self.dtype = dtype
        self._session = None
        self._quantization = {}

    def transcribe(self, chunk):
        import numpy as np
        audio = np.frombuffer(chunk.pcm, dtype='<i2').astype(np.float32) / 32768
        if not len(audio) or np.max(np.abs(audio)) < self.silence:
            return TranscriptSegment(chunk.start, chunk.start + chunk.duration, '')
        import mlx.core as mx
        if self._session is None:
            from huggingface_hub import snapshot_download
            from mlx_qwen3_asr import Session
            path = Path(self.model_name).expanduser()
            if not path.is_dir():
                path = Path(snapshot_download(self.model_name, local_files_only=True))
            quantization = path / 'quantization_config.json'
            if quantization.exists():
                self._quantization = json.loads(quantization.read_text())
            dtype = mx.float16 if self.dtype == 'auto' else getattr(mx, self.dtype)
            self._session = Session(model=str(path), dtype=dtype)
        result = self._session.transcribe((audio, chunk.sample_rate), language=self.language, max_new_tokens=512)
        mx.synchronize()
        return TranscriptSegment(chunk.start, chunk.start + chunk.duration, result.text.strip())

    def metrics(self):
        if self._session is None:
            return {}
        import mlx.core as mx
        return {'backend': 'mlx', 'dtype': str(self._session.dtype),
                'quantization_bits': self._quantization.get('bits'),
                'audio_encoder_bits': self._quantization.get('audio_tower_bits', self._quantization.get('bits')),
                'tensor_bytes': mx.get_active_memory(), 'cache_bytes': mx.get_cache_memory(),
                'peak_bytes': mx.get_peak_memory()}
