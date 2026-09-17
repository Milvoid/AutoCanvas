"""Explicit application use cases. Leaf modules never import this module."""
import asyncio
import json
import shutil
from pathlib import Path
from . import media
from .execution import blocking
from .outputs import Transcript, atomic_json
from .types import AuthenticationRequired, MediaError


def lecture_key(course_id, kind, lecture_id):
    return f'{course_id}:{kind}:{lecture_id}'


class CatalogSync:
    def __init__(self, canvas, video_factory, store, course_ids=()):
        self.canvas, self.video_factory, self.store = canvas, video_factory, store
        self.course_ids = set(course_ids)

    def courses(self):
        courses = self.canvas.courses()
        for course in courses:
            course['active'] = not self.course_ids or course['id'] in self.course_ids
            self.store.put('courses', course['id'], course)
        current = {c['id'] for c in courses}
        for old in self.store.list('courses'):
            if old['id'] not in current:
                old['active'] = False
                self.store.put('courses', old['id'], old)
        return courses

    def videos(self, course_id):
        client = self.video_factory(course_id)
        try:
            teaching_class = client.context()
            result = []
            for kind in ('vod', 'live'):
                rows = client.lectures(course_id, teaching_class, kind)
                for row in rows:
                    self.store.put('lectures', lecture_key(course_id, kind, row['id']), row)
                result.extend(rows)
            return result
        finally:
            client.close()

    def sources(self, lecture):
        # Each operation gets fresh course-scoped credentials. Nothing is cached in the DB.
        for attempt in range(2):
            client = self.video_factory(lecture['course_id'])
            try:
                return client.sources(lecture['id'], lecture['kind'])
            except AuthenticationRequired:
                if attempt:
                    raise
            finally:
                client.close()


class AssignmentSync:
    def __init__(self, canvas, session_factory, store, root):
        self.canvas, self.session_factory, self.store, self.root = canvas, session_factory, store, root

    def run(self, course_id):
        from .assignments import export
        rows = self.canvas.assignments(course_id)
        failed = False
        with self.session_factory() as session:
            for row in rows:
                result = export(session, row, self.root/str(course_id)/str(row['id']))
                result['course_id'] = str(course_id)
                self.store.put('assignments', f"{course_id}:{row['id']}", result)
                failed |= any(a['status'] == 'failed' for a in result['attachments'])
        if failed:
            raise IOError('Some attachments failed; successful attachments retained')
        return self.root/str(course_id)


async def transcribe_source(source, transcribe, folder, *, chunk_seconds=3, duration=None):
    output = Transcript(folder)
    reader = media.audio(source, offset=output.end, duration=max(0, duration-output.end) if duration else None, chunk_seconds=chunk_seconds)
    try:
        async for chunk in reader:
            output.append(await transcribe(chunk))
    finally:
        await reader.aclose()
        output.finish()
    return folder/'transcript.json'


async def slides_source(source, folder, cache, *, sample_every=5, duration=None):
    from .slides import extract
    if cache.exists():
        shutil.rmtree(cache)
    cache.mkdir(parents=True)
    pending = folder/'pending'
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True, exist_ok=True)
    try:
        await media.sample_frames(source, cache, every=sample_every, duration=duration)
        rows = await blocking(extract, cache, pending, sample_every=sample_every)
        serial = []
        for row in rows:
            row = dict(row)
            row['image'] = Path(row['image']).name
            row.pop('frame', None)
            serial.append(row)
        atomic_json(pending/'slides.json', serial)
        target = folder/'result'
        backup = folder/'previous'
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            target.rename(backup)
        pending.rename(target)
        if backup.exists():
            shutil.rmtree(backup)
        return target/'slides.json'
    finally:
        shutil.rmtree(cache, ignore_errors=True)


class Replay:
    def __init__(self, resolve_sources, transcribe, root, cache, *, chunk_seconds=3, sample_every=5):
        self.resolve_sources, self.transcribe = resolve_sources, transcribe
        self.root, self.cache = root, cache
        self.chunk_seconds, self.sample_every = chunk_seconds, sample_every

    async def run(self, lecture, kind, options):
        sources = await self.resolve_sources(lecture)
        purpose = 'audio' if kind == 'vod_asr' else 'screen'
        source = await media.select(sources, purpose, options.get('view'))
        folder = self.root/lecture['course_id']/lecture['id']/kind
        if kind == 'vod_asr':
            return await transcribe_source(source, self.transcribe, folder, chunk_seconds=self.chunk_seconds, duration=options.get('duration'))
        return await slides_source(source, folder, self.cache/lecture['course_id']/lecture['id'], sample_every=self.sample_every, duration=options.get('duration'))
