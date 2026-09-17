"""Dedicated live monitor: owns connection/retry/stop, not authentication or ASR."""
import asyncio
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from . import media
from .outputs import Transcript, append_event
from .types import AuthenticationRequired, MediaError


class KeywordDetector:
    def __init__(self, words, debounce=30):
        self.words = tuple(words)
        self.debounce = debounce
        self.last = {}

    def matches(self, text, now):
        found = []
        for word in self.words:
            if word in text and now-self.last.get(word, float('-inf')) >= self.debounce:
                found.append(word)
                self.last[word] = now
        return found


class LiveMonitor:
    def __init__(self, resolve_sources, transcribe, *, queue_chunks=100, chunk_seconds=3, keywords=(), debounce=30,
                 reconnect_seconds=5, reader=media.audio, selector=media.select):
        self.resolve_sources = resolve_sources
        self.transcribe = transcribe
        self.queue_chunks = queue_chunks
        self.chunk_seconds = chunk_seconds
        self.keywords, self.debounce = keywords, debounce
        self.reconnect_seconds = reconnect_seconds
        self.reader, self.selector = reader, selector

    async def run(self, lecture, folder: Path, *, view=None):
        end = datetime.fromisoformat(lecture['end']).timestamp()
        if end <= time.time():
            return None
        begin = datetime.fromisoformat(lecture['begin']).timestamp()
        transcript = Transcript(folder)
        events = folder/'events.jsonl'
        queue = asyncio.Queue(maxsize=self.queue_chunks)
        detector = KeywordDetector(self.keywords, self.debounce)
        connected = False
        append_event(events, {'type': 'monitor_start', 'at': time.time(), 'resume_after': transcript.end})

        async def produce():
            nonlocal connected
            while time.time() < end:
                try:
                    async with asyncio.timeout(max(0.01, end-time.time())):
                        sources = await self.resolve_sources(lecture)
                        source = await self.selector(sources, 'audio', view)
                        offset = max(0, time.time()-begin, transcript.end)
                        reader = self.reader(source, chunk_seconds=self.chunk_seconds)
                        append_event(events, {'type': 'connected', 'at': time.time(), 'offset': offset})
                        try:
                            async for chunk in reader:
                                connected = True
                                chunk = replace(chunk, start=offset+chunk.start)
                                if queue.full():
                                    lost = queue.get_nowait()
                                    queue.task_done()
                                    append_event(events, {'type': 'audio_gap', 'reason': 'queue_full', 'start': lost.start, 'end': lost.start+lost.duration})
                                queue.put_nowait(chunk)
                        finally:
                            await reader.aclose()
                except AuthenticationRequired:
                    raise
                except Exception as error:
                    append_event(events, {'type': 'connection_interrupted', 'at': time.time(), 'error': type(error).__name__})
                remaining = end-time.time()
                if remaining > 0:
                    await asyncio.sleep(min(self.reconnect_seconds, remaining))
            await queue.put(None)

        async def consume():
            while True:
                chunk = await queue.get()
                try:
                    if chunk is None:
                        return
                    segment = await self.transcribe(chunk, live=True)
                    transcript.append(segment)
                    for word in detector.matches(segment.text, time.monotonic()):
                        append_event(events, {'type': 'keyword', 'keyword': word, 'start': segment.start, 'text': segment.text})
                finally:
                    queue.task_done()

        producer = asyncio.create_task(produce(), name='live-reader')
        consumer = asyncio.create_task(consume(), name='live-transcription')
        group = asyncio.gather(producer, consumer)
        try:
            await asyncio.shield(group)
            if not connected:
                raise MediaError('No live audio received before scheduled end')
            return transcript.finish()
        except asyncio.CancelledError:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            if not consumer.done():
                if queue.full():
                    lost = queue.get_nowait()
                    queue.task_done()
                    append_event(events, {'type': 'audio_gap', 'reason': 'stop_drain', 'start': lost.start})
                queue.put_nowait(None)
                try:
                    await asyncio.wait_for(asyncio.shield(consumer), 30)
                except Exception:
                    pass
            raise
        finally:
            for task in (producer, consumer):
                task.cancel()
            await asyncio.gather(producer, consumer, return_exceptions=True)
            await asyncio.gather(group, return_exceptions=True)
            if not queue.empty():
                append_event(events, {'type': 'audio_gap', 'reason': 'monitor_stopped', 'queued_chunks': queue.qsize()})
            transcript.finish()
            append_event(events, {'type': 'monitor_stop', 'at': time.time()})
