"""New video platform client. Credentials are supplied, never acquired here."""
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from .types import MediaSource, AuthenticationRequired, RemoteError, VideoUnavailable, timestamp

BASE = 'https://v.sjtu.edu.cn/jy-application-resourcemanage'


class Video:
    def __init__(self, credential):
        self.session = credential.session
        self.token = credential.token

    def close(self):
        self.session.close()

    def get(self, path, **params):
        response = self.session.get(BASE + path, params=params, headers={'jwt-token': self.token}, timeout=30)
        if response.status_code in (401, 403):
            raise AuthenticationRequired('Video token expired')
        if response.status_code != 200:
            raise RemoteError(f'Video HTTP {response.status_code}')
        try:
            body = response.json()
        except ValueError:
            raise RemoteError('Video response is not JSON') from None
        if body.get('status') in (401, 403) or body.get('code') in (401, 403):
            raise AuthenticationRequired('Video token expired')
        if body.get('code') in ('LMS_COURSE_SIS_ID_MISSING', 'LMS_TEACHING_CLASS_NOT_FOUND'):
            raise VideoUnavailable(str(body['code']))
        if body.get('status') != 200:
            raise RemoteError('Video API rejected request: ' + str(body.get('code')))
        return body.get('data')

    def context(self):
        data = self.get('/lms/launch-context')
        if not data or not data.get('canvasRecord', {}).get('teachingClassId'):
            raise RemoteError('No teaching class mapped to this course')
        return data['canvasRecord']['teachingClassId']

    def lectures(self, course_id, teaching_class, kind):
        path = '/v1/subject_vod_list_new' if kind == 'vod' else '/v1/vod_live/t-1'
        params = {'page.pageSize': 100, 'page.orders[0].field': 'courBeginTime', 'page.orders[0].asc': 'true'}
        if kind == 'vod':
            params.update(teclIds=teaching_class, schoolOpenStatusFlag='false')
        else:
            # Same query as the official player; no assumption that liveDay means lookahead.
            params.update(teclId=teaching_class, liveDay=0)
        result = []
        page = 1
        while True:
            data = self.get(path, **params, **{'page.pageIndex': page})
            if not isinstance(data, dict) or not isinstance(data.get('records'), list):
                raise RemoteError('Invalid lecture list')
            for item in data['records']:
                result.append({'id': str(item['id']), 'course_id': str(course_id), 'teaching_class': str(teaching_class), 'kind': kind,
                               'name': item.get('subjName', ''), 'begin': timestamp(item.get('courBeginTime')),
                               'end': timestamp(item.get('courEndTime')), 'status': item.get('vodStatus' if kind == 'vod' else 'liveStatus')})
            if page >= int(data.get('pageCount') or 1):
                break
            page += 1
        return result

    def sources(self, lecture_id, kind='vod'):
        if kind == 'vod':
            info = self.get('/v1/course_vod_urls_new', courseId=lecture_id)
            return [MediaSource(v['url'], view=str(v.get('viewNum', 'unknown'))) for v in (info or {}).get('courseVodViewList', []) if v.get('url')]
        info = self.get('/v1/course_vod_videoinfos', courseId=lecture_id, playType='hls')
        sources = []
        for item in (info or {}).get('courseDeviceViewDtoList') or []:
            url = item.get('chanNameMainPlayUrl')
            if not url:
                continue
            parsed = urlsplit(url)
            if parsed.scheme not in ('https', 'http'):
                continue
            query = dict(parse_qsl(parsed.query))
            if item.get('mainTokenStr'):
                query['account_token'] = item['mainTokenStr']
            sources.append(MediaSource(urlunsplit(parsed._replace(query=urlencode(query))), view=str(item.get('deviViewNum', 'unknown'))))
        return sources
