import asyncio
import os
import time
import unittest

from autocanvas.asr_worker import ProcessRecognizer
from autocanvas.execution import Inference
from autocanvas.config import Settings
from autocanvas.types import AudioChunk, TranscriptSegment


def fake_worker(connection, options):
    try:
        while True:
            chunk = connection.recv()
            if chunk is None:
                return
            if chunk.start == -1:
                os._exit(2)
            if chunk.start == -2:
                time.sleep(30)
            if chunk.start == -3:
                connection.send((False, 'ValueError', {}))
                return
            if chunk.start == 9:
                time.sleep(.2)
            connection.send((True, TranscriptSegment(chunk.start, chunk.start + chunk.duration, 'ok'),
                             {'dtype': 'fake'}))
    except EOFError:
        pass
    finally:
        connection.close()


def chunk(start=0):
    return AudioChunk(b'\x00\x10' * 160, 16000, start)


class WorkerTests(unittest.TestCase):
    def worker(self, **kwargs):
        worker = ProcessRecognizer('fake', 'cpu', worker_target=fake_worker, **kwargs)
        self.addCleanup(worker.close)
        return worker

    def test_silence_never_starts_model(self):
        worker = self.worker()
        self.assertEqual(worker.transcribe(AudioChunk(b'\0\0' * 160, 16000, 0)).text, '')
        self.assertIsNone(worker.snapshot()['worker_pid'])

    def test_reuses_child_and_reaps_on_close(self):
        worker = self.worker()
        worker.transcribe(chunk())
        pid = worker.snapshot()['worker_pid']
        self.assertEqual(worker.transcribe(chunk(1)).text, 'ok')
        self.assertEqual(worker.snapshot()['worker_pid'], pid)
        worker.close()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        worker.transcribe(chunk(2))
        self.assertNotEqual(worker.snapshot()['worker_pid'], pid)

    def test_crash_and_model_error_recover_on_next_request(self):
        worker = self.worker()
        for start in [-1, -3]:
            with self.assertRaises((EOFError, RuntimeError)):
                worker.transcribe(chunk(start))
            self.assertIsNone(worker.snapshot()['worker_pid'])
            self.assertEqual(worker.transcribe(chunk()).text, 'ok')

    def test_timeout_reaps_child(self):
        worker = self.worker(timeout=.5)
        with self.assertRaises(TimeoutError):
            worker.transcribe(chunk(-2))
        self.assertIsNone(worker.snapshot()['worker_pid'])
        self.assertEqual(worker.transcribe(chunk()).text, 'ok')

    def test_settings_validation(self):
        for kw in [{'asr_backend': 'invalid'}, {'asr_dtype': 'int4'}, {'asr_idle_seconds': 0}, {'asr_idle_seconds': True}]:
            with self.assertRaises(ValueError):
                Settings(**kw).validate()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_unloads_and_next_request_restarts(self):
        worker = ProcessRecognizer('fake', 'cpu', worker_target=fake_worker)
        inference = Inference(worker.transcribe, idle_seconds=.05)
        try:
            await inference.transcribe(chunk())
            pid = worker.snapshot()['worker_pid']
            for _ in range(100):
                if worker.snapshot()['worker_pid'] is None:
                    break
                await asyncio.sleep(.02)
            self.assertIsNone(worker.snapshot()['worker_pid'])
            self.assertEqual((await inference.transcribe(chunk(1))).text, 'ok')
            self.assertNotEqual(worker.snapshot()['worker_pid'], pid)
        finally:
            await inference.close()

    async def test_shutdown_settles_inflight_and_queued_callers(self):
        worker = ProcessRecognizer('fake', 'cpu', worker_target=fake_worker)
        inference = Inference(worker.transcribe)
        await inference.transcribe(chunk())
        active = asyncio.create_task(inference.transcribe(chunk(9)))
        queued = asyncio.create_task(inference.transcribe(chunk(1)))
        await asyncio.sleep(.05)
        await inference.close()
        results = await asyncio.wait_for(asyncio.gather(active, queued, return_exceptions=True), 2)
        self.assertTrue(all(isinstance(r, asyncio.CancelledError) for r in results))
        self.assertIsNone(worker.snapshot()['worker_pid'])
