import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from autocanvas.mlx_asr import Recognizer
from autocanvas.asr_worker import _serve
from autocanvas.types import AudioChunk, TranscriptSegment


class MLXTests(unittest.TestCase):
    def test_offline_quantized_session_reuse_and_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'quantization_config.json').write_text(json.dumps({'bits': 4, 'audio_tower_bits': 8}))
            mx = types.ModuleType('mlx.core')
            mx.float16 = 'float16'
            mx.synchronize = Mock()
            mx.get_active_memory = Mock(return_value=500)
            mx.get_cache_memory = Mock(return_value=100)
            mx.get_peak_memory = Mock(return_value=800)
            mlx = types.ModuleType('mlx');mlx.core = mx
            session = Mock(dtype='float16')
            session.transcribe.return_value = types.SimpleNamespace(text=' 签到 ')
            package = types.ModuleType('mlx_qwen3_asr');package.Session = Mock(return_value=session)
            hub = types.ModuleType('huggingface_hub');hub.snapshot_download = Mock(return_value=tmp)
            with patch.dict('sys.modules', {'mlx': mlx, 'mlx.core': mx, 'mlx_qwen3_asr': package, 'huggingface_hub': hub}):
                recognizer = Recognizer('example/quantized-model')
                chunk = AudioChunk(b'\x00\x10' * 160, 16000, 7)
                for _ in range(2):
                    result = recognizer.transcribe(chunk)
                    self.assertEqual((result.start, result.end, result.text), (7, 7.01, '签到'))
                hub.snapshot_download.assert_called_once_with('example/quantized-model', local_files_only=True)
                package.Session.assert_called_once_with(model=tmp, dtype='float16')
                self.assertEqual(session.transcribe.call_args.kwargs, {'language': 'Chinese', 'max_new_tokens': 512})
                audio, rate = session.transcribe.call_args.args[0]
                self.assertEqual(rate, 16000)
                self.assertEqual(len(audio), 160)
                self.assertEqual(recognizer.metrics()['audio_encoder_bits'], 8)
                self.assertEqual(recognizer.metrics()['quantization_bits'], 4)

    def test_silence_does_not_load_runtime(self):
        recognizer = Recognizer('not-downloaded')
        self.assertEqual(recognizer.transcribe(AudioChunk(b'\0\0'*160,16000,0)).text, '')
        self.assertEqual(recognizer.metrics(), {})

    def test_worker_selects_mlx_backend(self):
        connection = Mock()
        chunk = AudioChunk(b'\x00\x10'*160, 16000, 0)
        connection.recv.side_effect = [chunk, None]
        recognizer = Mock()
        recognizer.transcribe.return_value = TranscriptSegment(0, .01, 'ok')
        recognizer.metrics.return_value = {'backend': 'mlx'}
        with patch('autocanvas.mlx_asr.Recognizer', return_value=recognizer) as factory:
            _serve(connection, {'backend': 'mlx', 'model': 'local-model', 'dtype': 'auto'})
        factory.assert_called_once_with(model='local-model', dtype='auto')
        connection.send.assert_called_once_with((True, recognizer.transcribe.return_value, {'backend':'mlx'}))
        connection.close.assert_called_once()
