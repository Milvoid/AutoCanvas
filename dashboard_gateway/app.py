import asyncio
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit
from aiohttp import web, ClientSession, ClientTimeout

COOKIE = 'autocanvas_dashboard'
LOGIN = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AutoCanvas · 登录</title><style>
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; min-height: 100dvh;
  display: grid; place-items: center; padding: 32px 20px;
  background: #f4efe8; color: #302b28;
  font-family: system-ui, -apple-system, "PingFang SC", sans-serif;
  isolation: isolate;
}
.fluid-background {
  position: fixed; inset: 0; z-index: -1; overflow: hidden; pointer-events: none;
  background: radial-gradient(ellipse at 75% 18%, #fffdf8 0, transparent 58%), #f3ede4;
}
.fluid-background svg {
  width: 100%; height: 100%; display: block;
  animation: fluid-drift 28s ease-in-out infinite alternate;
  transform-origin: center;
}
.login {
  width: min(420px, 100%); padding: 40px 36px 28px;
  background: rgb(255 253 249 / 92%);
  border: 1px solid rgb(255 255 255 / 85%); border-radius: 20px;
  box-shadow: 0 24px 80px -32px rgb(100 67 43 / 24%), 0 2px 8px rgb(100 67 43 / 4%);
}
h1 { font-size: 26px; font-weight: 600; margin: 0 0 12px; line-height: 1.35; }
p { color: #70655d; font-size: 14px; line-height: 1.7; }
.login > p { margin: 0; }
label { display: block; margin: 28px 0 8px; font-size: 14px; }
input, button { width: 100%; min-height: 46px; padding: 12px 14px; border-radius: 8px; font: inherit; font-size: 14px; }
input { border: 1px solid #b0a298; background: #fffdfa; color: #302b28; }
button { margin-top: 16px; border: 0; background: #302b28; color: #faf9f7; cursor: pointer; transition: background-color 150ms; }
button:hover { background: #4b413b; }
button:disabled { opacity: .5; cursor: wait; }
input:focus-visible, button:focus-visible { outline: 2px solid #9f5038; outline-offset: 3px; }
#error { color: #a13e32; min-height: 24px; margin: 12px 0 0; font-size: 14px; }
@keyframes fluid-drift {
  from { transform: scale(1.04) translate(-.7%, -.5%); }
  to { transform: scale(1.1) translate(.7%, .5%); }
}
@media (max-width: 480px) {
  body { padding: 24px 20px; }
  .login { padding: 32px 24px 20px; border-radius: 16px; }
}
@media (prefers-reduced-motion: reduce) {
  .fluid-background svg { animation: none; }
  button { transition: none; }
}
</style>
<div class="fluid-background" aria-hidden="true">
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1440 1000" preserveAspectRatio="xMidYMid slice" focusable="false">
    <defs>
      <linearGradient id="sand" x1="0" y1="0" x2="1" y2="1">
        <stop stop-color="#cbb798"/><stop offset=".5" stop-color="#e4d2ba"/><stop offset="1" stop-color="#f7f0e6"/>
      </linearGradient>
      <linearGradient id="clay" x1="0" y1="0" x2="1" y2=".7">
        <stop stop-color="#f1dfcf"/><stop offset=".55" stop-color="#d8b298"/><stop offset="1" stop-color="#c9957e"/>
      </linearGradient>
      <linearGradient id="silk" x1="0" y1="1" x2=".9" y2="0">
        <stop stop-color="#faf5ed"/><stop offset=".55" stop-color="#fdfbf5"/><stop offset="1" stop-color="#e5d7c3"/>
      </linearGradient>
      <filter id="soft" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="20"/></filter>
    </defs>
    <g filter="url(#soft)">
      <path fill="url(#sand)" d="M-180-130H1480C1260 55 1120 68 940 54 657 29 714 359 448 410 199 458 203 692-150 774Z"/>
      <path fill="url(#clay)" d="M1600 104C1302 183 1402 406 1099 435 794 465 1007 722 671 785 446 827 552 1110 229 1140H1600Z"/>
      <path fill="url(#silk)" d="M-150 1057C125 783 386 947 530 650 640 422 904 664 1068 479 1226 301 1197 122 1535 126L1560-10C1218-54 1100 185 952 302 735 475 583 276 420 547 263 807 54 670-170 832Z"/>
    </g>
  </svg>
</div>
<main class="login"><h1>AutoCanvas</h1><p>输入访问密码以登录。</p><form id="form"><label for="password">访问密码</label><input id="password" name="password" type="password" autocomplete="current-password" required autofocus maxlength="1024" aria-describedby="error"><button id="submit">登录</button><p id="error" role="alert"></p></form></main><script>
const form=document.getElementById('form'),button=document.getElementById('submit'),error=document.getElementById('error');form.addEventListener('submit',async e=>{e.preventDefault();button.disabled=true;error.textContent='';try{const r=await fetch('/_dashboard/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('password').value})});if(r.ok){location.replace('/');return}error.textContent=r.status===429?'尝试过于频繁，请稍后再试。':'密码不正确，请重试。'}catch{error.textContent='暂时无法连接，请稍后再试。'}finally{button.disabled=false}});
</script></html>'''


def create_app(config):
    password = config.get('password')
    if not isinstance(password, str) or not 12 <= len(password) <= 1024 or password.startswith('CHANGE-ME'):
        raise ValueError('dashboard.yml 中 password 至少需要 12 个字符')
    root = Path(config['dist']).resolve()
    if not (root/'index.html').is_file():raise ValueError('请先构建 WebUI')
    ttl = int(config.get('session_hours', 12))*3600
    if not 3600 <= ttl <= 7*86400:raise ValueError('session_hours 必须为 1 到 168')
    salt = secrets.token_bytes(16)
    def digest(value):return hashlib.scrypt(value.encode(), salt=salt, n=16384, r=8, p=1)
    expected = digest(password)
    sessions = {}
    attempts = defaultdict(deque)
    global_attempts = deque()
    login_lock = asyncio.Lock()
    target = config.get('autocanvas_api', 'http://127.0.0.1:8080')
    u = urlsplit(target)
    if u.scheme != 'http' or u.hostname not in ('127.0.0.1', 'localhost', '::1') or u.path not in ('', '/'):
        raise ValueError('内部 API 必须为本机 HTTP 地址')

    def identity(request):return request.cookies.get(COOKIE, '')
    def authenticated(request):return sessions.get(identity(request), 0) > time.monotonic()
    def same_origin(request):
        origin = request.headers.get('Origin')
        return (not origin or urlsplit(origin).netloc == request.host) and request.headers.get('Sec-Fetch-Site') != 'cross-site'

    @web.middleware
    async def guard(request, handler):
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not same_origin(request):
            return web.json_response({'error':'跨站请求被拒绝'}, status=403)
        if request.path in ('/_dashboard/login', '/_dashboard/logout'):
            return await handler(request)
        if not authenticated(request):
            if request.path in ('/', '/login') or ('text/html' in request.headers.get('Accept','') and not request.path.startswith(('/api/', '/assets/', '/health'))):
                return web.Response(text=LOGIN, content_type='text/html', headers={'Cache-Control':'no-store'})
            return web.json_response({'error':'请先输入访问密码'}, status=401,
                                     headers={'X-Dashboard-Auth':'required','Cache-Control':'no-store'})
        return await handler(request)

    @web.middleware
    async def security_headers(request, handler):
        response = await handler(request)
        if not response.prepared:
            response.headers['X-Frame-Options'] = 'DENY'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Referrer-Policy'] = 'same-origin'
            response.headers['Cache-Control'] = 'no-store'
        return response

    app = web.Application(middlewares=[security_headers, guard], client_max_size=2*1024**3)
    client = None
    streams = {}
    stopping = False

    async def login(request):
        if request.method == 'GET':
            return web.Response(text=LOGIN, content_type='text/html', headers={'Cache-Control':'no-store'})
        # Bound work and memory; do not trust spoofable forwarded IP headers.
        now = time.monotonic()
        for key in list(attempts):
            while attempts[key] and now-attempts[key][0] > 300:attempts[key].popleft()
            if not attempts[key]:del attempts[key]
        while global_attempts and now-global_attempts[0] > 60:global_attempts.popleft()
        key = request.remote or 'unknown'
        if len(attempts[key]) >= 10 or len(global_attempts) >= 60 or len(attempts) > 1000:
            return web.json_response({'error':'请稍后重试'}, status=429, headers={'Retry-After':'60'})
        attempts[key].append(now);global_attempts.append(now)
        if request.content_length is None or request.content_length > 4096:
            raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=request.content_length or 4097)
        try:data = await request.json()
        except Exception:return web.json_response({'error':'无效请求'},status=400)
        value = data.get('password') if isinstance(data, dict) else None
        if not isinstance(value,str) or len(value)>1024:return web.json_response({'error':'无效请求'},status=400)
        async with login_lock:
            valid = hmac.compare_digest(await asyncio.to_thread(digest,value), expected)
        if not valid:return web.json_response({'error':'密码不正确'},status=401)
        for token, expiry in list(sessions.items()):
            if expiry <= now:sessions.pop(token,None)
        if len(sessions) >= 256:sessions.pop(next(iter(sessions)))
        sessions.pop(identity(request),None)
        token = secrets.token_urlsafe(32);sessions[token] = time.monotonic()+ttl
        response = web.json_response({'authenticated':True}, headers={'Cache-Control':'no-store'})
        secure = request.secure or request.headers.get('X-Forwarded-Proto','').lower() == 'https'
        response.set_cookie(COOKIE,token,httponly=True,secure=secure,samesite='Strict',max_age=ttl,path='/')
        return response

    async def logout(request):
        token = identity(request);sessions.pop(token,None)
        for task, sid in list(streams.items()):
            if sid == token:task.cancel()
        response = web.HTTPSeeOther('/')
        response.del_cookie(COOKIE,path='/')
        response.headers['Cache-Control']='no-store'
        return response

    async def proxy(request):
        if any(request.path == prefix or request.path.startswith(prefix + '/')
               for prefix in ('/api/codex', '/api/homework')):
            return web.json_response({'error':'接口不存在'}, status=404)
        hop = {'host','cookie','connection','keep-alive','proxy-authenticate','proxy-authorization',
               'te','trailer','transfer-encoding','upgrade','content-length','forwarded','x-forwarded-for','x-forwarded-proto'}
        headers = {k:v for k,v in request.headers.items() if k.lower() not in hop}
        headers['Host'] = request.host
        headers['Accept-Encoding'] = 'identity'
        task = asyncio.current_task();streams[task] = identity(request)
        try:
            async with client.request(request.method, target.rstrip('/')+request.raw_path, headers=headers,
                                      data=request.content.iter_chunked(65536) if request.can_read_body else None,
                                      allow_redirects=False) as upstream:
                output = {k:v for k,v in upstream.headers.items() if k.lower() not in hop|{'set-cookie'}}
                output['Cache-Control']='no-store'
                response = web.StreamResponse(status=upstream.status,headers=output)
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):
                    if not authenticated(request):break
                    await response.write(chunk)
                return response
        except (ConnectionError, OSError, asyncio.TimeoutError):
            return web.json_response({'error':'目标服务暂不可用'},status=502)
        finally:streams.pop(task,None)

    async def static(request):
        if request.method not in ('GET','HEAD'):raise web.HTTPMethodNotAllowed(request.method,['GET','HEAD'])
        name = request.path.lstrip('/')
        path = (root/name).resolve()
        if not path.is_relative_to(root):raise web.HTTPNotFound()
        if not path.is_file():
            if name.startswith('assets/') or Path(name).suffix:raise web.HTTPNotFound()
            path = root/'index.html'
        return web.FileResponse(path,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

    async def lifetime(app):
        nonlocal client, stopping
        client = ClientSession(timeout=ClientTimeout(total=None,sock_connect=5),auto_decompress=False)
        async def reap():
            while not stopping:
                await asyncio.sleep(1)
                for task,token in list(streams.items()):
                    if sessions.get(token,0) <= time.monotonic():task.cancel()
        reaper = asyncio.create_task(reap())
        yield
        stopping=True;reaper.cancel()
        for task in list(streams):task.cancel()
        await asyncio.gather(reaper, *list(streams), return_exceptions=True)
        await client.close()
    app.cleanup_ctx.append(lifetime)
    app.router.add_get('/_dashboard/login',login)
    app.router.add_post('/_dashboard/login',login)
    app.router.add_post('/_dashboard/logout',logout)
    app.router.add_route('*','/api/{rest:.*}',proxy)
    app.router.add_route('*','/health',proxy)
    app.router.add_route('*','/{rest:.*}',static)
    return app
