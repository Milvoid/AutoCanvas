"""Application-level inference arbitration and cancellable blocking calls."""
import asyncio
import itertools


async def blocking(function, *args, **kwargs):
    """Do not leave a thread mutating files after its async owner has exited."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


class Inference:
    """Serial model access; live chunks precede queued replay chunks."""
    def __init__(self, recognize, *, idle_seconds=120):
        self.recognize = recognize
        self.idle_seconds = idle_seconds
        self.queue = asyncio.PriorityQueue()
        self.sequence = itertools.count()
        self.worker = None
        self.busy = False

    async def transcribe(self, chunk, *, live=False):
        if self.worker is None:
            self.worker = asyncio.create_task(self._consume())
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((0 if live else 1, next(self.sequence), chunk, future))
        return await future

    async def _consume(self):
        while True:
            try:
                _, _, chunk, future = await asyncio.wait_for(self.queue.get(), self.idle_seconds)
            except asyncio.TimeoutError:
                await self.release()
                continue
            try:
                if future.cancelled():
                    continue
                self.busy = True
                result = await blocking(self.recognize, chunk)
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                future.cancel()
                raise
            except Exception as error:
                if not future.done():
                    future.set_exception(error)
            finally:
                self.busy = False
                self.queue.task_done()

    async def release(self):
        owner = getattr(self.recognize, '__self__', None)
        close = getattr(owner, 'close', None)
        if close is not None:
            await blocking(close)

    def snapshot(self):
        owner = getattr(self.recognize, '__self__', None)
        snapshot = getattr(owner, 'snapshot', None)
        return {'busy': self.busy, 'queued': self.queue.qsize(),
                'idle_seconds': self.idle_seconds, **(snapshot() if snapshot else {})}

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        while not self.queue.empty():
            *_, future = self.queue.get_nowait()
            future.cancel()
            self.queue.task_done()
        await self.release()
