import ast
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from autocanvas.types import AudioChunk, TranscriptSegment, MediaSource, timestamp
from autocanvas.storage import Store
from autocanvas.outputs import Transcript
from autocanvas.config import Settings
from autocanvas.execution import Inference
from autocanvas.live import LiveMonitor, KeywordDetector
from autocanvas import media
from autocanvas.video import Video


class Boundaries(unittest.TestCase):
    def test_imports_are_lightweight(self):
        code = "import autocanvas.auth, autocanvas.video, autocanvas.asr; import sys; assert 'torch' not in sys.modules; assert 'cv2' not in sys.modules; assert 'autocanvas.storage' not in sys.modules"
        subprocess.run([sys.executable, '-c', code], check=True)

    def test_no_reverse_dependencies(self):
        allowed = {'asr': {'types'}, 'media': {'types'}, 'slides': set(), 'auth': {'types', '_jaccount'},
                   'video': {'types'}, 'canvas': {'types'}, '_jaccount': set()}
        for module, imports in allowed.items():
            tree = ast.parse(Path('autocanvas', module+'.py').read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    names = {node.module.split('.')[0]} if node.module else {a.name for a in node.names}
                    self.assertLessEqual(names, imports, module)
        for path in Path('autocanvas').glob('*.py'):
            self.assertNotIn('AutoCanvasGateway', path.read_text())
            self.assertNotIn('JobRegistry', path.read_text())

    def test_time_normalization(self):
        self.assertTrue(timestamp('2026-09-17 12:00:00').endswith('+08:00'))
        self.assertEqual(timestamp(0), '1970-01-01T08:00:00+08:00')


class Persistence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'state.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_concurrent_claim_and_idempotence(self):
        run = self.store.enqueue('vod_asr','1','2')
        self.assertEqual(run, self.store.enqueue('vod_asr','1','2'))
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.store.claim('vod_asr'))) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(r is not None for r in results), 1)
        self.store.recover()
        self.assertEqual(self.store.execution(run)['status'], 'pending')

    def test_cancel_is_terminal_until_retry(self):
        run = self.store.enqueue('vod_asr','1','2')
        self.store.claim('vod_asr')
        self.store.cancel(run)
        self.store.finish(run,'succeeded')
        self.assertEqual(self.store.execution(run)['status'], 'cancelled')
        self.assertTrue(self.store.retry(run))
        self.assertIsNotNone(self.store.claim('vod_asr'))

    def test_pause_filters_automatic_only(self):
        self.store.enqueue('vod_asr','1','2', options={'automatic': True})
        manual = self.store.enqueue('vod_asr','1','3')
        self.assertEqual(self.store.claim('vod_asr', allow_automatic=False)['id'], manual)
        self.assertIsNone(self.store.claim('vod_asr', allow_automatic=False))

    def test_transcript_resume_and_torn_tail(self):
        folder = Path(self.temp.name)/'out'
        out = Transcript(folder)
        out.append(TranscriptSegment(0, 3, 'hello'))
        with out.path.open('a') as f: f.write('{broken')
        resumed = Transcript(folder)
        self.assertEqual(resumed.end, 3)
        resumed.append(TranscriptSegment(0, 3, 'duplicate'))
        resumed.append(TranscriptSegment(3, 4, 'tail'))
        rows = json.loads(resumed.finish().read_text())
        self.assertEqual([r['text'] for r in rows], ['hello','tail'])


class VideoTests(unittest.TestCase):
    def test_pages_and_source_fields(self):
        video = Video(SimpleNamespace(session=None, token='private'))
        calls = []
        def get(path, **params):
            calls.append(params)
            page = params['page.pageIndex']
            return {'pageCount':2, 'records':[{'id':page, 'subjName':'Test','courBeginTime':'2026-09-17 12:00:00'}]}
        video.get = get
        rows = video.lectures('1',2,'vod')
        self.assertEqual([r['id'] for r in rows], ['1','2'])
        self.assertEqual(len(calls),2)
        video.get = lambda *a, **k: {'courseDeviceViewDtoList':[{'chanNameMainPlayUrl':'https://example.test/live.m3u8?a=1','mainTokenStr':'a&b','deviViewNum':5}]}
        sources = video.sources('2','live')
        self.assertIn('account_token=a%26b', sources[0].location)
        self.assertNotIn('account_token', repr(sources[0]))


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_inference_priority_and_cancellation(self):
        calls=[]
        gate=threading.Event()
        started=threading.Event()
        def recognize(chunk):
            calls.append(chunk.start)
            if chunk.start == 0:
                started.set()
                gate.wait(3)
            return TranscriptSegment(chunk.start,chunk.start+1,'ok')
        inference=Inference(recognize)
        one=asyncio.create_task(inference.transcribe(AudioChunk(b'00',1,0)))
        await asyncio.to_thread(started.wait,2)
        replay=asyncio.create_task(inference.transcribe(AudioChunk(b'00',1,1)))
        live=asyncio.create_task(inference.transcribe(AudioChunk(b'00',1,2),live=True))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(one,replay,live)
        await inference.close()
        self.assertEqual(calls,[0,2,1])

    async def test_live_overflow_reconnect_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            connections=[]
            closed=[]
            async def resolve(lecture):
                connections.append(1)
                return [MediaSource('fake')]
            async def select(sources,*args):return sources[0]
            async def reader(source,**kw):
                try:
                    for i in range(20):
                        yield AudioChunk(b'\0\0'*160,16000,i*.01)
                        await asyncio.sleep(.001)
                finally:closed.append(1)
            async def recognize(chunk,**kw):
                await asyncio.sleep(.01)
                return TranscriptSegment(chunk.start,chunk.start+.01,'签到')
            now=datetime.now(timezone.utc)
            monitor=LiveMonitor(resolve,recognize,queue_chunks=2,reconnect_seconds=.01,reader=reader,selector=select,keywords=['签到'])
            path=await monitor.run({'begin':now.isoformat(),'end':(now+timedelta(seconds=.15)).isoformat()},Path(tmp))
            self.assertTrue(path.exists())
            events=[json.loads(l) for l in (Path(tmp)/'events.jsonl').read_text().splitlines()]
            self.assertTrue(any(e['type']=='audio_gap' for e in events))
            self.assertGreater(len(connections),1)
            self.assertEqual(len(connections),len(closed))
            self.assertEqual(sum(e['type']=='keyword' for e in events),1)

    async def test_live_cancel_closes_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            closed=asyncio.Event()
            async def resolve(lecture):return [MediaSource('fake')]
            async def select(sources,*args):return sources[0]
            async def reader(source,**kw):
                try:
                    while True:
                        yield AudioChunk(b'00'*16,16000,0)
                        await asyncio.sleep(.01)
                finally:closed.set()
            async def recognize(chunk,**kw):return TranscriptSegment(chunk.start,chunk.start+.01,'')
            now=datetime.now(timezone.utc)
            monitor=LiveMonitor(resolve,recognize,reader=reader,selector=select)
            task=asyncio.create_task(monitor.run({'begin':now.isoformat(),'end':(now+timedelta(hours=1)).isoformat()},Path(tmp)))
            await asyncio.sleep(.04)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertTrue(closed.is_set())

    async def test_real_ffmpeg_tail_and_local_slides(self):
        from autocanvas.flows import slides_source
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'test.mp4'
            await media.command(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=white:size=640x360:rate=10','-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','3.25','-c:v','mpeg4','-c:a','aac',str(source)])
            chunks=[c async for c in media.audio(MediaSource(str(source)),chunk_seconds=3)]
            self.assertEqual(len(chunks),2)
            self.assertLess(chunks[-1].duration,1)
            self.assertGreater(chunks[-1].duration,0)
            result=await slides_source(MediaSource(str(source)),root/'slides',root/'frames',sample_every=1)
            self.assertEqual(len(json.loads(result.read_text())),1)
            self.assertFalse((root/'frames').exists())

class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_timeout_reaps_process(self):
        created=[]
        original=asyncio.create_subprocess_exec
        async def capture(*args,**kwargs):
            proc=await original(*args,**kwargs)
            created.append(proc)
            return proc
        with patch('autocanvas.media.asyncio.create_subprocess_exec',capture):
            with self.assertRaises(asyncio.TimeoutError):
                await media.command([sys.executable,'-c','import time;time.sleep(30)'],timeout=.05)
        self.assertEqual(len(created),1)
        self.assertIsNotNone(created[0].returncode)

    async def test_audio_cancellation_reaps_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tone.wav'
            await media.command(['ffmpeg','-y','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','10',str(path)])
            created=[]
            original=asyncio.create_subprocess_exec
            async def capture(*args,**kwargs):
                proc=await original(*args,**kwargs)
                created.append(proc)
                return proc
            with patch('autocanvas.media.asyncio.create_subprocess_exec',capture):
                reader=media.audio(MediaSource(str(path)),realtime=True)
                task=asyncio.create_task(anext(reader))
                await asyncio.sleep(.1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                await reader.aclose()
            self.assertIsNotNone(created[0].returncode)


class AuthenticationTests(unittest.TestCase):
    def test_lti_chain_and_scope(self):
        from autocanvas.auth import Auth
        class Response:
            status_code=200
            def __init__(self,url,text=''):self.url,self.text=url,text
            def raise_for_status(self):pass
        class Session:
            closed=False
            def get(self,*args,**kwargs):
                return Response('https://oc.sjtu.edu.cn/courses/1/external_tools/8329','<form action="https://v.sjtu.edu.cn/jy-lti-adapter/lti/canvas/oidc/login-initiation/canvas-record"><input name="login_hint" value="x"></form>')
            def post(self,url,**kwargs):
                if 'login-initiation' in url:
                    return Response('https://oc.sjtu.edu.cn/api/lti/authorize','<form action="https://v.sjtu.edu.cn/jy-lti-adapter/lti/canvas/launch/canvas-record"><input name="id_token" value="secret"></form>')
                return Response('https://v.sjtu.edu.cn/jy-application-resourcemanage-ui/#/lms/launch?jwt_token=private')
            def close(self):self.closed=True
        auth=Auth(Path('not-used'));session=Session()
        with patch.object(auth,'session',return_value=session):
            credential=auth.video('1')
            self.assertEqual(credential.token,'private')
            self.assertNotIn('private',repr(credential))
            self.assertFalse(session.closed)

    def test_video_business_error_not_http_success(self):
        from autocanvas.types import VideoUnavailable, AuthenticationRequired
        class Response:
            status_code=200
            def __init__(self,body):self.body=body
            def json(self):return self.body
        session=SimpleNamespace(get=lambda *a,**k:Response({'status':500,'code':'LMS_COURSE_SIS_ID_MISSING'}))
        client=Video(SimpleNamespace(session=session,token='secret'))
        with self.assertRaises(VideoUnavailable):client.context()
        session.get=lambda *a,**k:Response({'status':401})
        with self.assertRaises(AuthenticationRequired):client.context()

class AttachmentTests(unittest.TestCase):
    def test_attachment_success_is_reused_and_failure_retries(self):
        from autocanvas.assignments import export
        class Response:
            status_code=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_content(self,size):yield b'attachment bytes'
        class Session:
            calls=[]
            fail=True
            def get(self,url,**kwargs):
                self.calls.append(url)
                if url.endswith('/2') and self.fail:
                    raise IOError('temporary')
                return Response()
        assignment={'id':1,'name':'Exercise','updated_at':'today','description':'<p>正文</p>',
                    'attachments':[{'id':1,'display_name':'../a.pdf','url':'https://example.test/1'},
                                   {'id':2,'display_name':'b.pdf','url':'https://example.test/2'}]}
        with tempfile.TemporaryDirectory() as tmp:
            session=Session();folder=Path(tmp)/'output'
            first=export(session,assignment,folder)
            self.assertEqual([a['status'] for a in first['attachments']],['succeeded','failed'])
            session.fail=False
            second=export(session,assignment,folder)
            self.assertEqual([a['status'] for a in second['attachments']],['succeeded','succeeded'])
            self.assertEqual(session.calls.count('https://example.test/1'),1)
            self.assertFalse((Path(tmp)/'a.pdf').exists())
            self.assertEqual((folder/'description.txt').read_text(),'正文')

    def test_canvas_html_attachments_are_discovered(self):
        from autocanvas.assignments import attachments
        rows=attachments({'description':'<a href="/courses/1/files/25/download?download_frd=1">Week 1.pdf</a>'})
        self.assertEqual(rows[0]['id'],'25')
        self.assertEqual(rows[0]['url'],'https://oc.sjtu.edu.cn/files/25/download')
