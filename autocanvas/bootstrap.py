"""Composition root; the only place that assembles concrete module dependencies."""
import fcntl
from contextlib import contextmanager
from .auth import Auth
from .canvas import Canvas
from .video import Video
from .storage import Store
from .asr import Recognizer
from .execution import Inference, blocking
from .flows import CatalogSync, AssignmentSync, Replay
from .live import LiveMonitor
from .service import Service


@contextmanager
def service_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root/'service.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another service owns this runtime; use its HTTP API') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def build(settings):
    auth = Auth(settings.root/'auth'/'canvas_session.json')
    canvas = Canvas(auth.session)
    store = Store(settings.root/'state.sqlite3')
    catalog = CatalogSync(canvas, lambda course: Video(auth.video(course)), store, settings.course_ids)
    assignments = AssignmentSync(canvas, auth.session, store, settings.root/'assignments')
    inference = Inference(Recognizer(settings.model, settings.device).transcribe)

    async def resolve(lecture):
        return await blocking(catalog.sources, lecture)

    replay = Replay(resolve, inference.transcribe, settings.root/'outputs', settings.root/'cache', chunk_seconds=settings.chunk_seconds, sample_every=settings.sample_every)
    monitor = LiveMonitor(resolve, inference.transcribe, queue_chunks=settings.live_queue_chunks, chunk_seconds=settings.chunk_seconds,
                          keywords=settings.keywords, debounce=settings.keyword_debounce)
    service = Service(settings, store, catalog, assignments, replay, monitor)
    return service, inference
