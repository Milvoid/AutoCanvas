"""Local audio -> text; no networking, media decoding, scheduling or persistence."""
from threading import Lock
from .types import AudioChunk, TranscriptSegment


class Recognizer:
    def __init__(self, model='Qwen/Qwen3-ASR-0.6B', device='mps', language='Chinese', silence=0.005):
        self.model_name, self.device, self.language, self.silence = model, device, language, silence
        self._model = None
        self._lock = Lock()

    def transcribe(self, chunk: AudioChunk):
        import numpy as np
        audio = np.frombuffer(chunk.pcm, dtype='<i2').astype(np.float32) / 32768
        if not len(audio) or np.max(np.abs(audio)) < self.silence:
            return TranscriptSegment(chunk.start, chunk.start + chunk.duration, '')
        with self._lock:
            if self._model is None:
                from qwen_asr import Qwen3ASRModel
                self._model = Qwen3ASRModel.from_pretrained(self.model_name, device_map=self.device, local_files_only=True)
            result = self._model.transcribe(audio=(audio, chunk.sample_rate), language=self.language)
        return TranscriptSegment(chunk.start, chunk.start + chunk.duration, result[0].text.strip() if result and result[0].text else '')
