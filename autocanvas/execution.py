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
    def __init__(self, recognize):
        self.recognize = recognize
        self.queue = asyncio.PriorityQueue()
        self.sequence = itertools.count()
        self.worker = None

    async def transcribe(self, chunk, *, live=False):
        if self.worker is None:
            self.worker = asyncio.create_task(self._consume())
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((0 if live else 1, next(self.sequence), chunk, future))
        return await future

    async def _consume(self):
        while True:
            _, _, chunk, future = await self.queue.get()
            try:
                if future.cancelled():
                    continue
                result = await blocking(self.recognize, chunk)
                if not future.done():
                    future.set_result(result)
            except Exception as error:
                if not future.done():
                    future.set_exception(error)
            finally:
                self.queue.task_done()

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        while not self.queue.empty():
            *_, future = self.queue.get_nowait()
            future.cancel()
            self.queue.task_done()
