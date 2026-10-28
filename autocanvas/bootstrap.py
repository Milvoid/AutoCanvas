"""Composition root; the only place that assembles concrete module dependencies."""
import os
from contextlib import contextmanager
from .auth import Auth
from .canvas import Canvas
from .video import Video
from .storage import Store
from .asr_worker import ProcessRecognizer
from .execution import Inference, blocking
from .flows import CatalogSync, AssignmentSync, Replay
from .live import LiveMonitor
from .service import Service


@contextmanager
def service_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root/'service.lock').open('a') as handle:
        try:
            if os.name == 'nt':
                import msvcrt
                if handle.tell() == 0:
                    handle.write('\0')
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('Another service owns this runtime; use its HTTP API') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def build(settings):
    settings.validate()
    auth = Auth(settings.root/'auth'/'canvas_session.json')
    canvas = Canvas(auth.session)
    store = Store(settings.root/'state.sqlite3')
    catalog = CatalogSync(canvas, lambda course: Video(auth.video(course)), store, settings.course_ids)
    assignments = AssignmentSync(canvas, auth.session, store, settings.root/'assignments')
    inference = Inference(ProcessRecognizer(settings.model, settings.device, dtype=settings.asr_dtype, backend=settings.asr_backend).transcribe, idle_seconds=settings.asr_idle_seconds)

    async def resolve(lecture):
        return await blocking(catalog.sources, lecture)

    replay = Replay(resolve, inference.transcribe, settings.root/'outputs', settings.root/'cache', chunk_seconds=settings.chunk_seconds, sample_every=settings.sample_every)
    monitor = LiveMonitor(resolve, inference.transcribe, queue_chunks=settings.live_queue_chunks, chunk_seconds=settings.chunk_seconds,
                          keywords=settings.keywords, debounce=settings.keyword_debounce)
    service = Service(settings, store, catalog, assignments, replay, monitor)
    service.inference = inference
    from .configuration import Configuration
    from .browser_auth import BrowserAuth

    def apply_idle(values, changed):
        if changed & {'model', 'device', 'asr_dtype', 'asr_backend'}:
            inference.recognize.__self__.close()
            inference.recognize = ProcessRecognizer(values['model'], values['device'], dtype=values['asr_dtype'], backend=values['asr_backend']).transcribe
        inference.idle_seconds = values['asr_idle_seconds']
        replay.chunk_seconds, replay.sample_every = values['chunk_seconds'], values['sample_every']
        service.monitor = LiveMonitor(resolve, inference.transcribe, queue_chunks=values['live_queue_chunks'],
                                      chunk_seconds=values['chunk_seconds'], keywords=values['keywords'], debounce=values['keyword_debounce'])

    def apply_hot():
        catalog.course_ids = set(settings.course_ids)
        for course in store.list('courses'):
            course['active'] = not settings.course_ids or course['id'] in settings.course_ids
            store.put('courses', course['id'], course)

    service.configuration = Configuration(settings, lambda: bool(service.active) or inference.busy or not inference.queue.empty(), apply_idle, apply_hot)
    from .attendance import MobileLogin
    from .live_attendance import LiveAttendance
    async def resolve_attendance(lecture, protocol):
        return await blocking(catalog.sources, lecture, live_protocol=protocol)
    service.attendance = LiveAttendance(store, settings.root, resolve_attendance, MobileLogin(auth.session))
    service.browser_auth = BrowserAuth(auth)
    return service, inference
