import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from aiohttp.test_utils import TestServer, TestClient
from autocanvas.config import Settings
from autocanvas.storage import Store
from autocanvas.service import Service
from autocanvas.http import create_app
from autocanvas.types import AuthenticationRequired


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.settings=Settings(root=Path(self.tmp.name),schedule_interval=1)
        self.store=Store(self.settings.root/'state.sqlite3')
        self.store.put('courses','1',{'id':'1','active':True})
        self.calls=[]
        async def replay(lecture,kind,options):
            self.calls.append(kind)
            if options.get('auth_error'):raise AuthenticationRequired()
            if options.get('wait'):await asyncio.sleep(60)
            return Path('artifact')
        self.service=Service(self.settings,self.store,None,None,SimpleNamespace(run=replay),None)
        self.lecture={'id':'2','course_id':'1','kind':'vod'}
        self.store.put('lectures','1:vod:2',self.lecture)

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def wait_status(self,run,status):
        async with asyncio.timeout(3):
            while self.store.execution(run)['status'] != status:
                await asyncio.sleep(.01)

    async def test_cancel_does_not_kill_worker(self):
        run=self.service.enqueue_processing('1','2','vod_asr',{'wait':True})
        await self.service.start(automation=False)
        await self.wait_status(run,'running')
        self.service.cancel(run)
        await asyncio.sleep(.05)
        second=self.store.enqueue('vod_asr','1','3')
        self.store.put('lectures','1:vod:3',{**self.lecture,'id':'3'})
        await self.wait_status(second,'succeeded')
        self.assertEqual(self.store.execution(run)['status'],'cancelled')

    async def test_auth_failure_does_not_block_other_processing(self):
        bad=self.service.enqueue_processing('1','2','vod_asr',{'auth_error':True})
        good=self.service.enqueue_processing('1','2','vod_slides')
        await self.service.start(automation=False)
        await self.wait_status(bad,'needs_login')
        await self.wait_status(good,'succeeded')

    async def test_shutdown_recovers_interrupted(self):
        run=self.service.enqueue_processing('1','2','vod_asr',{'wait':True})
        await self.service.start(automation=False)
        await self.wait_status(run,'running')
        await self.service.close()
        self.assertEqual(self.store.execution(run)['status'],'pending')

    async def test_tick_idempotent_and_pause(self):
        self.store.put('sync','scheduled',{'at':time.time()})
        now=datetime.now(timezone.utc)
        self.store.put('lectures','1:live:4',{'id':'4','course_id':'1','kind':'live','begin':(now+timedelta(minutes=5)).isoformat(),'end':(now+timedelta(hours=1)).isoformat()})
        await self.service.tick()
        await self.service.tick()
        self.assertEqual(len(self.store.executions()),3)
        self.store.put('control','automation',{'paused':True})
        await self.service.start(automation=False)
        await asyncio.sleep(.1)
        self.assertEqual(self.calls,[])
        manual=self.service.enqueue_processing('1','2','vod_asr',force=True)
        await self.wait_status(manual,'succeeded')

    async def test_http_control_and_validation(self):
        client=TestClient(TestServer(create_app(self.service)))
        await client.start_server()
        try:
            r=await client.get('/health');self.assertEqual(r.status,200)
            r=await client.post('/api/process/vod_asr',json={'course_id':'1','lecture_id':'2'})
            self.assertEqual(r.status,202)
            run=(await r.json())['execution_id']
            r=await client.post(f'/api/executions/{run}/cancel')
            self.assertTrue((await r.json())['changed'])
            r=await client.patch('/api/courses/1/rules',json={'asr':False})
            self.assertEqual((await r.json())['asr'],False)
            r=await client.post('/api/automation',json={'paused':'yes'})
            self.assertEqual(r.status,400)
            r=await client.post('/api/process/vod_asr',json={'course_id':'1','lecture_id':'unknown'})
            self.assertEqual(r.status,400)
            r=await client.get('/plugins');self.assertEqual(r.status,404)
        finally:
            await client.close()
