"""Outer automation controller. Fixed workflows; no plugin or job registry."""
import asyncio
import logging
import time
from datetime import datetime
from .execution import blocking
from .flows import lecture_key
from .types import AuthenticationRequired, VideoUnavailable

log = logging.getLogger(__name__)


class Service:
    def __init__(self, settings, store, catalog, assignments, replay, monitor):
        self.settings, self.store = settings, store
        self.catalog, self.assignments, self.replay, self.monitor = catalog, assignments, replay, monitor
        self.active = {}
        self.background = []
        self.stopping = False

    def enabled(self, course, feature):
        course_row = self.store.get('courses', str(course), {})
        if not course_row.get('active', False):
            return False
        control = self.store.get('control', 'automation', {})
        if control.get('paused', False):
            return False
        rules = self.store.get('rules', str(course), {})
        defaults = {'asr': self.settings.auto_asr, 'slides': self.settings.auto_slides, 'live': self.settings.auto_live}
        return rules.get(feature, defaults[feature])

    def request_sync(self, course_id='*', *, automatic=False):
        return self.store.enqueue('sync', course_id, force=True, options={'automatic': automatic})

    def enqueue_processing(self, course_id, lecture_id, kind, options=None, force=False):
        lecture = self.store.get('lectures', lecture_key(course_id, 'live' if kind == 'live' else 'vod', lecture_id))
        if lecture is None:
            raise KeyError('Unknown lecture; synchronize first')
        return self.store.enqueue(kind, course_id, lecture_id, options=options, force=force)

    async def start(self, *, automation=True):
        self.store.recover()
        self.stopping = False
        for kind in ('sync', 'assignments', 'vod_asr', 'vod_slides', 'live'):
            self.background.append(asyncio.create_task(self._worker(kind), name=f'worker:{kind}'))
        if automation:
            self.background.append(asyncio.create_task(self._schedule(), name='automation'))

    async def close(self):
        self.stopping = True
        for task in self.background:
            task.cancel()
        for task in list(self.active.values()):
            task.cancel()
        await asyncio.gather(*self.background, *list(self.active.values()), return_exceptions=True)
        self.background.clear()
        self.active.clear()

    def cancel(self, run_id):
        changed = self.store.cancel(run_id)
        task = self.active.get(run_id)
        if task:
            task.cancel()
        return changed

    async def _sync(self, course_id, automatic=False):
        last = self.store.get('sync', 'courses', {}).get('at', 0)
        if not self.store.list('courses') or time.time()-last >= self.settings.course_interval:
            await blocking(self.catalog.courses)
            self.store.put('sync', 'courses', {'at': time.time()})
        courses = [c for c in self.store.list('courses') if c.get('active') and (course_id == '*' or c['id'] == course_id)]
        failed = []
        for course in courses:
            try:
                await blocking(self.catalog.videos, course['id'])
                self.store.put('sync', course['id'], {'course_id': course['id'], 'at': time.time(), 'status': 'succeeded'})
            except AuthenticationRequired:
                raise
            except VideoUnavailable:
                self.store.put('sync', course['id'], {'course_id':course['id'], 'at': time.time(), 'status':'video_unavailable'})
            except Exception as error:
                failed.append(course['id'])
                self.store.put('sync', course['id'], {'course_id': course['id'], 'at': time.time(), 'status': 'failed', 'error': type(error).__name__})
            self.store.enqueue('assignments', course['id'], force=True, options={'automatic': automatic})
        if failed:
            raise RuntimeError('Video synchronization failed for some courses; see sync status')

    async def _execute(self, row):
        run_id = row['id']
        try:
            artifact = None
            if row['kind'] == 'sync':
                await self._sync(row['course_id'], row['options'].get('automatic', False))
            elif row['kind'] == 'assignments':
                artifact = await blocking(self.assignments.run, row['course_id'])
            else:
                kind = 'live' if row['kind'] == 'live' else 'vod'
                lecture = self.store.get('lectures', lecture_key(row['course_id'], kind, row['lecture_id']))
                if lecture is None:
                    raise LookupError('Lecture no longer available')
                if kind == 'live':
                    if not lecture.get('end') or datetime.fromisoformat(lecture['end']).timestamp() <= time.time():
                        self.store.finish(run_id, 'expired')
                        self.request_sync(row['course_id'])
                        return
                    folder = self.settings.root/'outputs'/row['course_id']/row['lecture_id']/'live'
                    artifact = await self.monitor.run(lecture, folder, view=row['options'].get('view'))
                    self.request_sync(row['course_id'])
                else:
                    artifact = await self.replay.run(lecture, row['kind'], row['options'])
            self.store.finish(run_id, 'succeeded', artifact=artifact)
        except asyncio.CancelledError:
            # Explicit cancellation remains cancelled; service shutdown becomes resumable.
            self.store.finish(run_id, 'pending')
            raise
        except AuthenticationRequired:
            self.store.finish(run_id, 'needs_login', error='AuthenticationRequired')
        except Exception as error:
            delay = (5, 15, 60)[row['attempts']-1] if row['attempts'] <= 3 else None
            self.store.finish(run_id, 'pending' if delay is not None else 'failed', error=type(error).__name__, delay=delay or 0)
            log.warning('%s failed: %s', row['kind'], type(error).__name__)
        finally:
            self.active.pop(run_id, None)

    async def _worker(self, kind):
        while True:
            paused = self.store.get('control', 'automation', {}).get('paused', False)
            row = self.store.claim(kind, allow_automatic=not paused)
            if row is None:
                await asyncio.sleep(0.5)
                continue
            if row['options'].get('automatic') and kind in ('vod_asr', 'vod_slides', 'live'):
                feature = {'vod_asr': 'asr', 'vod_slides': 'slides', 'live': 'live'}[kind]
                if not self.enabled(row['course_id'], feature):
                    self.store.defer(row['id'])
                    await asyncio.sleep(0.1)
                    continue
            task = asyncio.create_task(self._execute(row), name=f"{kind}:{row['id']}")
            self.active[row['id']] = task
            if kind != 'live':
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if self.stopping:
                        raise
                    # A user cancelled this execution, not the worker.
                    if not task.cancelled():
                        raise
            else:
                # Each live lecture has its own independently cancellable monitor.
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def tick(self):
        if self.store.get('control', 'automation', {}).get('paused', False):
            return
        active = {c['id'] for c in self.store.list('courses') if c.get('active')}
        now = time.time()
        last = self.store.get('sync', 'scheduled', {}).get('at', 0)
        if now-last >= self.settings.sync_interval:
            self.request_sync(automatic=True)
            self.store.put('sync', 'scheduled', {'at': now})
        for lecture in self.store.list('lectures'):
            course = lecture['course_id']
            if course not in active:
                continue
            if lecture['kind'] == 'vod':
                for feature, kind in [('asr','vod_asr'), ('slides','vod_slides')]:
                    if self.enabled(course, feature):
                        self.store.enqueue(kind, course, lecture['id'], options={'automatic': True})
            elif self.enabled(course, 'live') and lecture.get('begin') and lecture.get('end'):
                begin, end = (datetime.fromisoformat(lecture[k]).timestamp() for k in ('begin','end'))
                if begin-self.settings.live_lead_seconds <= now < end:
                    self.store.enqueue('live', course, lecture['id'], options={'automatic': True})

    async def _schedule(self):
        self.request_sync(automatic=True)
        while True:
            try:
                await self.tick()
            except Exception as error:
                log.error('Automation tick failed: %s', type(error).__name__)
            await asyncio.sleep(self.settings.schedule_interval)
