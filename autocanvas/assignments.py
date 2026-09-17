"""Assignment text and attachment handling; caller supplies authenticated session."""
import hashlib
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from .outputs import atomic_json
from .types import RemoteError


def html_to_text(html):
    return BeautifulSoup(html or '', 'html.parser').get_text('\n', strip=True)


def attachments(assignment):
    items = {str(a['id']): a for a in assignment.get('attachments', []) if a.get('id') and a.get('url')}
    for link in BeautifulSoup(assignment.get('description') or '', 'html.parser').find_all('a', href=True):
        url = urljoin('https://oc.sjtu.edu.cn', link['href'])
        if urlsplit(url).hostname != 'oc.sjtu.edu.cn':
            continue
        match = re.search(r'/files/(\d+)(?:/download)?', urlsplit(url).path)
        if match:
            file_id = match[1]
            items.setdefault(file_id, {'id': file_id, 'url': f'https://oc.sjtu.edu.cn/files/{file_id}/download', 'display_name': link.get_text(strip=True) or file_id})
    return list(items.values())


def export(session, assignment, folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    text = html_to_text(assignment.get('description'))
    temporary = folder/'description.txt.tmp'
    temporary.write_text(text)
    temporary.replace(folder/'description.txt')
    existing = {}
    manifest_path = folder/'assignment.json'
    if manifest_path.exists():
        import json
        old = json.loads(manifest_path.read_text())
        if old.get('updated_at') == assignment.get('updated_at'):
            existing = {a['id']: a for a in old.get('attachments', []) if a.get('status') == 'succeeded'}
    results = []
    for item in attachments(assignment):
        file_id = str(item['id'])
        name = re.sub(r'[^\w.\- ]', '_', Path(item.get('display_name', file_id)).name)[:160] or file_id
        path = folder/(re.sub(r'\W', '_', file_id)+'-'+name)
        if file_id in existing and path.exists():
            results.append(existing[file_id])
            continue
        temp = path.with_suffix(path.suffix+'.part')
        try:
            # Requests' domain-scoped cookie jar is preserved; never flatten cookies.
            with session.get(item['url'], stream=True, timeout=60) as response:
                if response.status_code != 200:
                    raise RemoteError('Attachment unavailable')
                digest = hashlib.sha256()
                with temp.open('wb') as handle:
                    for block in response.iter_content(65536):
                        handle.write(block)
                        digest.update(block)
            temp.replace(path)
            results.append({'id': file_id, 'name': name, 'path': str(path), 'sha256': digest.hexdigest(), 'status': 'succeeded'})
        except Exception as error:
            temp.unlink(missing_ok=True)
            results.append({'id': file_id, 'name': name, 'status': 'failed', 'error': type(error).__name__})
    result = {k: assignment.get(k) for k in ('id','name','updated_at','due_at','points_possible','html_url')}
    result.update(description=text, attachments=results)
    atomic_json(manifest_path, result)
    return result
