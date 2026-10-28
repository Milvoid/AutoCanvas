"""Thin HTTP adapter over explicit use cases. Never runs model code in handlers."""
from aiohttp import web
from urllib.parse import urlsplit


def create_app(service):
    @web.middleware
    async def errors(request, handler):
        try:
            origin = request.headers.get('Origin')
            if origin and urlsplit(origin).netloc != request.host:
                raise web.HTTPForbidden(text='Cross-origin requests are not allowed')
            return await handler(request)
        except ValueError as error:
            return web.json_response({'error': str(error)}, status=400)
        except (KeyError, TypeError):
            return web.json_response({'error': '请求参数不正确或资源不存在'}, status=400)
        except web.HTTPException:
            raise
        except Exception as error:
            return web.json_response({'error': type(error).__name__}, status=500)

    app = web.Application(middlewares=[errors], client_max_size=65536)
    store = service.store

    async def health(request):
        blocked = sum(r['status'] == 'needs_login' for r in store.executions())
        failed = [t.get_name() for t in service.background if t.done() and not t.cancelled() and t.exception()]
        return web.json_response({'status': 'degraded' if failed else 'needs_login' if blocked else 'ok', 'failed_workers': failed, 'version': 2,
                                  'scheduler_running': any(t.get_name() == 'automation' and not t.done() for t in service.background), 'active': len(service.active), 'asr': service.inference.snapshot() if hasattr(service, 'inference') else {}, 'automation': store.get('control','automation', {'paused': False})})

    async def listing(request):
        collection = request.match_info['collection']
        if collection not in ('courses', 'lectures', 'assignments', 'sync'):
            raise web.HTTPNotFound()
        rows = store.list(collection)
        if request.query.get('course_id'):
            rows = [r for r in rows if r.get('course_id', r.get('id')) == request.query['course_id']]
        return web.json_response(rows)

    async def sync(request):
        body = await request.json() if request.can_read_body else {}
        course = str(body.get('course_id', '*'))
        if course != '*' and store.get('courses', course) is None:
            raise KeyError(course)
        return web.json_response({'execution_id': service.request_sync(course)}, status=202)

    async def process(request):
        kind = request.match_info['kind']
        if kind not in ('vod_asr', 'vod_slides', 'live'):
            raise web.HTTPNotFound()
        body = await request.json()
        options = {'view': str(body['view'])} if 'view' in body else {}
        run_id = service.enqueue_processing(str(body['course_id']), str(body['lecture_id']), kind, options, force=body.get('retry') is True)
        return web.json_response({'execution_id': run_id}, status=202)

    async def executions(request):
        if 'id' in request.match_info:
            result = store.execution(request.match_info['id'])
            if not result:
                raise web.HTTPNotFound()
        else:
            result = store.executions()
        return web.json_response(result)

    async def control_execution(request):
        run_id = request.match_info['id']
        if not store.execution(run_id):
            raise web.HTTPNotFound()
        if request.match_info['action'] == 'cancel':
            changed = service.cancel(run_id)
        elif request.match_info['action'] == 'retry':
            changed = store.retry(run_id)
        else:
            raise web.HTTPNotFound()
        return web.json_response({'changed': changed})

    async def automation(request):
        if request.method == 'GET':
            return web.json_response(store.get('control','automation', {'paused': False}))
        body = await request.json()
        if set(body) != {'paused'} or type(body['paused']) is not bool:
            raise ValueError()
        service.set_automation(body['paused'])
        return web.json_response(body)

    async def rules(request):
        course_id = request.match_info['id']
        if store.get('courses', course_id) is None:
            raise KeyError(course_id)
        if request.method == 'GET':
            return web.json_response(store.get('rules', course_id, {}))
        body = await request.json()
        if set(body)-{'asr','slides','live'} or any(v is not None and type(v) is not bool for v in body.values()):
            raise ValueError()
        merged = {k: v for k, v in {**store.get('rules', course_id, {}), **body}.items() if v is not None}
        store.put('rules', course_id, merged)
        return web.json_response(merged)

    app.router.add_get('/health', health)
    app.router.add_get('/api/executions', executions)
    app.router.add_get('/api/executions/{id}', executions)
    app.router.add_post('/api/executions/{id}/{action}', control_execution)
    app.router.add_post('/api/sync', sync)
    app.router.add_post('/api/process/{kind}', process)
    app.router.add_get('/api/automation', automation)
    app.router.add_post('/api/automation', automation)
    app.router.add_get('/api/courses/{id}/rules', rules)
    app.router.add_patch('/api/courses/{id}/rules', rules)
    from .web_api import install
    install(app, service)
    app.router.add_get('/api/{collection}', listing)
    return app
