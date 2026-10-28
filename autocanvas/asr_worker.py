"""One disposable ASR process; model/native allocations never enter the web server."""
import array
import multiprocessing
from threading import Lock
from .types import TranscriptSegment


def _serve(connection, options):
    backend = options.pop('backend', 'torch')
    if backend == 'mlx':
        from .mlx_asr import Recognizer
    else:
        from .asr import Recognizer
    recognizer = Recognizer(**options)
    try:
        while True:
            try:
                chunk = connection.recv()
            except EOFError:
                break
            if chunk is None:
                break
            try:
                segment = recognizer.transcribe(chunk)
                connection.send((True, segment, recognizer.metrics()))
            except Exception as error:
                # No model prompts, audio, or paths in public diagnostics.
                connection.send((False, type(error).__name__, {}))
                break  # Discard potentially corrupt/OOM native state.
    finally:
        connection.close()


class ProcessRecognizer:
    def __init__(self, model, device, *, dtype='auto', backend='torch', timeout=180, worker_target=_serve):
        self.options = {'model': model, 'device': device, 'dtype': dtype, 'backend': backend}
        self.timeout = timeout
        self._target = worker_target
        self._lock = Lock()
        self._process = None
        self._connection = None
        self._metrics = {}

    def transcribe(self, chunk):
        # Avoid starting a multi-GB model for silence; no numpy/torch in the parent.
        samples = array.array('h', chunk.pcm)
        import sys
        if sys.byteorder != 'little':
            samples.byteswap()
        if not samples or max(abs(x) for x in samples) < 0.005 * 32768:
            return TranscriptSegment(chunk.start, chunk.start + chunk.duration, '')
        with self._lock:
            if self._process is None or not self._process.is_alive():
                self._close()
                context = multiprocessing.get_context('spawn')
                self._connection, child = context.Pipe()
                self._process = context.Process(target=self._target, args=(child, self.options),
                                                name='autocanvas-asr', daemon=True)
                try:
                    self._process.start()
                except BaseException:
                    self._connection.close()
                    self._connection = self._process = None
                    raise
                finally:
                    child.close()
            try:
                self._connection.send(chunk)
                if not self._connection.poll(self.timeout):
                    raise TimeoutError('ASR worker timed out')
                success, result, metrics = self._connection.recv()
                if not success:
                    raise RuntimeError('ASR worker failed: ' + result)
                self._metrics = metrics
                return result
            except BaseException:
                self._close()
                raise

    def snapshot(self):
        process = self._process
        try:
            running = process is not None and process.is_alive()
            pid = process.pid if running else None
        except ValueError:  # Concurrent idle shutdown has closed its process handle.
            running, pid = False, None
        return {'worker_pid': pid,
                'loaded': running and bool(self._metrics),
                **(self._metrics if running else {})}

    def _close(self):
        process, connection = self._process, self._connection
        self._process = self._connection = None
        self._metrics = {}
        if connection is not None:
            try:
                connection.send(None)
            except (OSError, EOFError):
                pass
            connection.close()
        if process is not None:
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(2)
            if process.is_alive():
                process.kill()
                process.join()
            process.close()

    def close(self):
        with self._lock:
            self._close()
