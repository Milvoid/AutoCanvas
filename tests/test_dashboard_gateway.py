import asyncio
import tempfile
import unittest
from pathlib import Path
from aiohttp import web, CookieJar
from aiohttp.test_utils import TestClient, TestServer
from dashboard_gateway.app import create_app, COOKIE


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'assets').mkdir();(self.root/'index.html').write_text('PRIVATE-DASHBOARD')
        (self.root/'assets'/'app.js').write_text('PRIVATE-BUNDLE')
        self.calls=[]
        async def upstream(request):
            self.calls.append((request.path,await request.read(),dict(request.headers)))
            return web.json_response({'path':request.path,'query':request.query_string})
        app=web.Application();app.router.add_route('*','/{path:.*}',upstream)
        self.upstream=TestServer(app);await self.upstream.start_server()
        self.config={'password':'test-password-123456','dist':str(self.root),
                     'autocanvas_api':str(self.upstream.make_url('')).rstrip('/')}
        self.app=create_app(self.config)
        self.client=TestClient(TestServer(self.app),cookie_jar=CookieJar(unsafe=True));await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close();await self.upstream.close();self.tmp.cleanup()
    async def login(self,headers=None):
        return await self.client.post('/_dashboard/login',json={'password':self.config['password']},headers=headers or {})

    async def test_no_assets_or_api_before_login(self):
        page=await self.client.get('/');text=await page.text()
        self.assertIn('访问密码',text);self.assertNotIn('PRIVATE',text);self.assertNotIn('<script src',text)
        for path in ['/assets/app.js','/api/courses','/api/assignments','/api/codex/sessions','/api/homework/health','/api/events','/api/file?path=auth/credentials.yml','/health','/index.html']:
            response=await self.client.get(path)
            self.assertEqual(response.status,401,path)
        self.assertEqual(self.calls,[])

    async def test_login_proxy_logout_and_cookie(self):
        bad=await self.client.post('/_dashboard/login',json={'password':'bad'});self.assertEqual(bad.status,401)
        good=await self.login();self.assertEqual(good.status,200)
        self.assertTrue(good.cookies[COOKIE]['httponly']);self.assertEqual(good.cookies[COOKIE]['samesite'],'Strict')
        self.assertEqual(await (await self.client.get('/')).text(),'PRIVATE-DASHBOARD')
        self.assertEqual(await (await self.client.get('/assets/app.js')).text(),'PRIVATE-BUNDLE')
        for path in ['/api/courses','/api/assignments','/health']:
            response=await self.client.get(path);self.assertEqual(response.status,200)
        self.assertNotIn('Cookie',self.calls[-1][2])
        await self.client.post('/_dashboard/logout')
        self.assertEqual((await self.client.get('/api/courses')).status,401)

    async def test_csrf_rejected(self):
        response=await self.login({'Origin':'https://evil.example'});self.assertEqual(response.status,403)
        await self.login()
        response=await self.client.post('/api/sync',json={},headers={'Origin':'https://evil.example'})
        self.assertEqual(response.status,403);self.assertEqual(self.calls,[])

    async def test_https_cookie_and_rate_limit(self):
        response=await self.login({'X-Forwarded-Proto':'https'})
        self.assertTrue(response.cookies[COOKIE]['secure'])
        for _ in range(10):response=await self.client.post('/_dashboard/login',json={'password':'wrong'})
        self.assertEqual(response.status,429)

    async def test_cookie_not_accepted_after_restart(self):
        await self.login();cookies=self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))
        other=TestClient(TestServer(create_app(self.config)));await other.start_server()
        try:
            response=await other.get('/api/courses',headers={'Cookie':f'{COOKIE}={cookies[COOKIE].value}'})
            self.assertEqual(response.status,401)
        finally:await other.close()

    async def test_proxy_body_query_and_origin_preserved(self):
        await self.login()
        response=await self.client.post('/api/assignments/sync?test=a%20b',json={'course_id':'hello'},headers={'Origin':str(self.client.make_url('')).rstrip('/')})
        self.assertEqual(response.status,200)
        self.assertIn(b'hello',self.calls[-1][1]);self.assertEqual(self.calls[-1][2]['Host'],self.client.make_url('/').authority)
        self.assertEqual((await response.json())['query'],'test=a b')

    async def test_removed_agent_apis_never_reach_upstream(self):
        await self.login()
        for method,path in [('GET','/api/codex'),('GET','/api/codex/sessions'),
                            ('POST','/api/codex/sessions'),('PATCH','/api/codex/config'),
                            ('POST','/api/codex/models/refresh'),
                            ('POST','/api/codex/sessions/example/turns'),
                            ('POST','/api/codex/sessions/example/stop'),
                            ('GET','/api/homework'),('GET','/api/homework/health'),
                            ('POST','/api/homework/sessions')]:
            with self.subTest(method=method,path=path):
                response=await self.client.request(method,path)
                self.assertEqual(response.status,404)
                self.assertEqual(await response.json(),{'error':'接口不存在'})
        self.assertEqual(self.calls,[])

    async def test_expiry_blocks_static_and_api(self):
        await self.login()
        from unittest.mock import patch
        from types import SimpleNamespace
        import time
        real_clock=time.monotonic
        with patch('dashboard_gateway.app.time',SimpleNamespace(monotonic=lambda:real_clock()+8*86400)):
            self.assertEqual((await self.client.get('/assets/app.js')).status,401)

    def test_missing_or_default_password_fails_closed(self):
        for value in ['',None,'CHANGE-ME-to-a-long-private-password']:
            with self.assertRaises(ValueError):create_app({**self.config,'password':value})

    async def test_sse_streams_incrementally_and_logout_closes_it(self):
        async def events(request):
            response=web.StreamResponse(headers={'Content-Type':'text/event-stream'})
            await response.prepare(request)
            try:
                for _ in range(100):
                    await response.write(b'data: fixture\n\n');await asyncio.sleep(.02)
            except (ConnectionResetError,asyncio.CancelledError):pass
            return response
        app=web.Application();app.router.add_get('/api/events',events)
        server=TestServer(app);await server.start_server()
        gateway=TestClient(TestServer(create_app({**self.config,'autocanvas_api':str(server.make_url('')).rstrip('/')})),cookie_jar=CookieJar(unsafe=True))
        await gateway.start_server()
        try:
            await gateway.post('/_dashboard/login',json={'password':self.config['password']})
            stream=await gateway.get('/api/events')
            self.assertEqual(await asyncio.wait_for(stream.content.readline(),1),b'data: fixture\n')
            await gateway.post('/_dashboard/logout')
            try:await asyncio.wait_for(stream.read(),1)
            except Exception as error:
                from aiohttp import ClientPayloadError
                self.assertIsInstance(error,ClientPayloadError)
            self.assertEqual((await gateway.get('/api/events')).status,401)
        finally:await gateway.close();await server.close()
