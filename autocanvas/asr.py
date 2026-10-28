"""Local audio -> text; no networking, media decoding, scheduling or persistence."""
from threading import Lock
import time
from .types import AudioChunk, TranscriptSegment


class Recognizer:
    def __init__(self, model='Qwen/Qwen3-ASR-0.6B', device='mps', language='Chinese', silence=0.005, dtype='auto'):
        self.model_name, self.device, self.language, self.silence = model, device, language, silence
        self.dtype = dtype
        self._model = None
        self._lock = Lock()
        self._last_trim = time.monotonic()

    def transcribe(self, chunk: AudioChunk):
        import numpy as np
        audio = np.frombuffer(chunk.pcm, dtype='<i2').astype(np.float32) / 32768
        if not len(audio) or np.max(np.abs(audio)) < self.silence:
            return TranscriptSegment(chunk.start, chunk.start + chunk.duration, '')
        with self._lock:
            import torch
            if self._model is None:
                from qwen_asr import Qwen3ASRModel
                # Preserve checkpoint precision instead of silently expanding BF16 to FP32.
                dtype = self.dtype if self.dtype == 'auto' else getattr(torch, self.dtype)
                self._model = Qwen3ASRModel.from_pretrained(self.model_name, device_map=self.device,
                                                          dtype=dtype, local_files_only=True)
            with torch.inference_mode():
                result = self._model.transcribe(audio=(audio, chunk.sample_rate), language=self.language)
            segment = TranscriptSegment(chunk.start, chunk.start + chunk.duration, result[0].text.strip() if result and result[0].text else '')
            # Coarse trimming avoids flushing the allocator on every 3-second chunk.
            if str(self.device).startswith('mps') and time.monotonic() - self._last_trim >= 60:
                torch.mps.empty_cache()
                self._last_trim = time.monotonic()
            return segment

    def metrics(self):
        if self._model is None:
            return {}
        import torch
        result = {'backend': 'torch', 'dtype': str(self._model.model.dtype)}
        if str(self.device).startswith('mps'):
            result.update(tensor_bytes=torch.mps.current_allocated_memory(),
                          driver_bytes=torch.mps.driver_allocated_memory())
        return result
