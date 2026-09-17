"""Canvas data access with an injected session factory; no authentication policy."""
from urllib.parse import urlsplit
from .types import AuthenticationRequired, RemoteError


class Canvas:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def _pages(self, path, params=None):
        result = []
        with self.session_factory() as session:
            url = 'https://oc.sjtu.edu.cn/api/v1/' + path
            while url:
                if urlsplit(url).hostname != 'oc.sjtu.edu.cn':
                    raise RemoteError('Unexpected Canvas pagination host')
                response = session.get(url, params=params, timeout=30)
                params = None
                if response.status_code in (401, 403) or 'jaccount' in response.url:
                    raise AuthenticationRequired('Canvas session expired')
                if response.status_code != 200:
                    raise RemoteError(f'Canvas HTTP {response.status_code}')
                try:
                    rows = response.json()
                except ValueError:
                    raise RemoteError('Canvas did not return JSON') from None
                if not isinstance(rows, list):
                    raise RemoteError('Unexpected Canvas list response')
                result.extend(rows)
                url = response.links.get('next', {}).get('url')
        return result

    def courses(self):
        return [{'id': str(c['id']), 'name': c.get('name', '')} for c in self._pages('courses', {'enrollment_state': 'active', 'per_page': 100}) if c.get('id')]

    def assignments(self, course_id):
        return self._pages(f'courses/{int(course_id)}/assignments', {'per_page': 100})
