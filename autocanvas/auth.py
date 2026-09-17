"""Authentication only: Canvas sessions and course-scoped LTI credentials."""
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from urllib.parse import urljoin, urlsplit, parse_qs
from bs4 import BeautifulSoup
from . import _jaccount
from .types import AuthenticationRequired


@dataclass
class VideoCredential:
    session: object = field(repr=False)
    token: str = field(repr=False)


class Auth:
    def __init__(self, session_file: Path):
        self.session_file = session_file
        self._lock = RLock()

    def session(self, *, interactive=False):
        with self._lock:
            try:
                return _jaccount.ensure_session(self.session_file, auto_prompt=interactive)
            except Exception:
                raise AuthenticationRequired("Canvas login required; run autocanvas login") from None

    def video(self, course_id: str):
        session = self.session()
        try:
            response = session.get(f"https://oc.sjtu.edu.cn/courses/{int(course_id)}/external_tools/8329", timeout=30)
            for _ in range(4):
                response.raise_for_status()
                url = urlsplit(response.url)
                token = parse_qs(url.fragment.partition("?")[2]).get("jwt_token")
                if url.hostname == "v.sjtu.edu.cn" and token:
                    return VideoCredential(session, token[0])
                forms = []
                for form in BeautifulSoup(response.text, "html.parser").find_all("form"):
                    action = urljoin(response.url, form.get("action", ""))
                    target = urlsplit(action)
                    if target.scheme == "https" and target.hostname == "v.sjtu.edu.cn" and target.path.startswith("/jy-lti-adapter/lti/canvas/"):
                        forms.append((form, action))
                if len(forms) != 1:
                    raise AuthenticationRequired("No recognized video launch form")
                form, action = forms[0]
                fields = {i['name']: i.get('value', '') for i in form.find_all('input', attrs={'name': True})}
                response = session.post(action, data=fields, timeout=30)
            raise AuthenticationRequired("Video launch failed")
        except Exception:
            session.close()
            raise AuthenticationRequired("Video launch failed; retry from Canvas") from None
