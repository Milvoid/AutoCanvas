"""Explicit commands for each capability; no service is needed for local processing."""
import argparse
import asyncio
import json
import logging
import signal
import shutil
from pathlib import Path
from .config import Settings


def parser():
    p = argparse.ArgumentParser(prog='autocanvas')
    p.add_argument('--config', type=Path)
    p.add_argument('--root', type=Path, help='Override private runtime directory')
    sub = p.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--import-session', type=Path)
    sub.add_parser('login')
    assignments = sub.add_parser('assignments')
    assignments.add_argument('course')
    serve = sub.add_parser('serve')
    serve.add_argument('--no-automation', action='store_true')
    sync = sub.add_parser('sync')
    sync.add_argument('--course')
    sync.add_argument('--assignments', action='store_true')
    ls = sub.add_parser('list')
    ls.add_argument('collection', choices=['courses','lectures','assignments','executions','sync'])
    src = sub.add_parser('sources')
    src.add_argument('course')
    src.add_argument('lecture')
    src.add_argument('--kind', choices=['vod','live'], default='vod')
    src.add_argument('--show-urls', action='store_true', help='Print private media addresses explicitly')
    for name in ('transcribe','slides'):
        cmd = sub.add_parser(name)
        cmd.add_argument('source')
        cmd.add_argument('--output', required=True, type=Path)
        cmd.add_argument('--duration', type=float)
    proc = sub.add_parser('process')
    proc.add_argument('course')
    proc.add_argument('lecture')
    proc.add_argument('--kind', choices=['vod_asr','vod_slides','both','live'], default='both')
    proc.add_argument('--view')
    proc.add_argument('--duration', type=float, help='Bounded replay verification; use a separate runtime for samples')
    proc.add_argument('--retry', action='store_true')
    return p


async def main_async(args, settings):
    from .auth import Auth
    from .bootstrap import build, service_lock
    from .types import MediaSource, VideoUnavailable
    if args.command == 'init':
        settings.root.mkdir(parents=True, exist_ok=True)
        settings.root.chmod(0o700)
        if args.import_session:
            destination = settings.root/'auth'/'canvas_session.json'
            if destination.exists():
                raise ValueError('Session file already exists')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(args.import_session, destination)
            destination.chmod(0o600)
        print('Initialized private runtime:', settings.root)
        return
    if args.command == 'login':
        from .execution import blocking
        session = await blocking(Auth(settings.root/'auth'/'canvas_session.json').session, interactive=True)
        session.close()
        print('Canvas session ready; retry needs_login executions through the API.')
        return
    if args.command in ('transcribe', 'slides'):
        from .flows import transcribe_source, slides_source
        from .asr import Recognizer
        from .execution import Inference
        if args.duration is not None and args.duration <= 0:
            raise ValueError('duration must be positive')
        source = MediaSource(args.source)
        if args.command == 'transcribe':
            inference = Inference(Recognizer(settings.model, settings.device).transcribe)
            try:
                output = await transcribe_source(source, inference.transcribe, args.output, chunk_seconds=settings.chunk_seconds, duration=args.duration)
            finally:
                await inference.close()
        else:
            output = await slides_source(source, args.output, args.output/'.frames', sample_every=settings.sample_every, duration=args.duration)
        print(output)
        return
    if args.command == 'list':
        from .storage import Store
        store = Store(settings.root/'state.sqlite3')
        rows = store.executions() if args.collection == 'executions' else store.list(args.collection)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    with service_lock(settings.root):
        service, inference = build(settings)
        try:
            if args.command == 'serve':
                from logging.handlers import TimedRotatingFileHandler
                log_dir = settings.root/'logs'
                log_dir.mkdir(parents=True, exist_ok=True)
                handler = TimedRotatingFileHandler(log_dir/'service.log', when='midnight', backupCount=14, encoding='utf-8')
                handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
                logging.getLogger().addHandler(handler)
                from aiohttp import web
                from .http import create_app
                runner = web.AppRunner(create_app(service), access_log=None)
                await runner.setup()
                try:
                    await web.TCPSite(runner, settings.host, settings.port).start()
                    await service.start(automation=not args.no_automation)
                    stopped = asyncio.Event()
                    loop = asyncio.get_running_loop()
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        loop.add_signal_handler(sig, stopped.set)
                    print(f'AutoCanvas V2 listening on http://{settings.host}:{settings.port}', flush=True)
                    await stopped.wait()
                finally:
                    await runner.cleanup()
            elif args.command == 'sync':
                from .execution import blocking
                courses = await blocking(service.catalog.courses)
                errors = []
                for course in courses:
                    if not course['active'] or args.course and course['id'] != args.course:
                        continue
                    try:
                        rows = await blocking(service.catalog.videos, course['id'])
                        print(json.dumps({'course':course['id'], 'lectures':len(rows)}, ensure_ascii=False))
                    except VideoUnavailable:
                        print(json.dumps({'course':course['id'], 'status':'video_unavailable'}))
                    except Exception as error:
                        errors.append(course['id'])
                        print(json.dumps({'course':course['id'], 'error':type(error).__name__}))
                    if args.assignments:
                        await blocking(service.assignments.run, course['id'])
                if errors:
                    raise RuntimeError('Some course video mappings unavailable')
            elif args.command == 'assignments':
                from .execution import blocking
                print(await blocking(service.assignments.run, args.course))
            elif args.command == 'sources':
                from .execution import blocking
                sources = await blocking(service.catalog.sources, {'course_id':args.course, 'id':args.lecture, 'kind':args.kind})
                print(json.dumps([{'view':s.view, **({'url':s.location} if args.show_urls else {})} for s in sources], ensure_ascii=False))
            elif args.command == 'process':
                from .execution import blocking
                from .flows import lecture_key
                if args.duration is not None and args.duration <= 0:
                    raise ValueError('duration must be positive')
                lecture_kind = 'live' if args.kind == 'live' else 'vod'
                if service.store.get('lectures', lecture_key(args.course, lecture_kind, args.lecture)) is None:
                    await blocking(service.catalog.videos, args.course)
                kinds = ['vod_asr','vod_slides'] if args.kind == 'both' else [args.kind]
                if args.duration is not None:
                    if args.kind == 'live':
                        raise ValueError('Use scheduled live end, not replay duration')
                    from .flows import Replay
                    sample = Replay(service.replay.resolve_sources, inference.transcribe,
                                    settings.root/'samples', settings.root/'sample_cache',
                                    chunk_seconds=settings.chunk_seconds, sample_every=settings.sample_every)
                    lecture = service.store.get('lectures', lecture_key(args.course, 'vod', args.lecture))
                    for kind in kinds:
                        result = await sample.run(lecture, kind, {'duration':args.duration, 'view':args.view})
                        print(json.dumps({'sample_only':True, 'kind':kind, 'artifact':str(result)}))
                    return
                for kind in kinds:
                    options = {k:v for k,v in {'view':args.view, 'duration':args.duration}.items() if v is not None}
                    run_id = service.enqueue_processing(args.course, args.lecture, kind, options, force=args.retry)
                    service.store.recover()
                    while True:
                        row = service.store.execution(run_id)
                        if row['status'] not in ('pending','running'):
                            break
                        claimed = service.store.claim(kind, run_id=run_id)
                        if claimed:
                            await service._execute(claimed)
                        else:
                            await asyncio.sleep(0.5)
                    print(json.dumps(service.store.execution(run_id), ensure_ascii=False))
                    if service.store.execution(run_id)['status'] != 'succeeded':
                        raise RuntimeError('Processing did not succeed')
        finally:
            await service.close()
            await inference.close()


def main():
    args = parser().parse_args()
    settings = Settings.load(args.config)
    if args.root:
        settings.root = args.root.resolve()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    try:
        asyncio.run(main_async(args, settings))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        # Requests exceptions can contain credential-bearing URLs.
        print(f'Failed: {type(error).__name__}; check authentication, configuration and execution status.')
        raise SystemExit(1)
