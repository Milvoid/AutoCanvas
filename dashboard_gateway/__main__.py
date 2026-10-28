import argparse
from pathlib import Path
import yaml
from aiohttp import web
from .app import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='dashboard.yml')
    args = parser.parse_args()
    path = Path(args.config).resolve()
    settings = yaml.safe_load(path.read_text())
    if not isinstance(settings, dict):raise ValueError('需要 dashboard.yml 配置')
    settings['dist'] = str((path.parent/settings.get('dist', 'webui/dist')).resolve())
    web.run_app(create_app(settings), host=settings.get('host', '127.0.0.1'),
                port=settings.get('port', 4173), access_log=None, shutdown_timeout=5)


if __name__ == '__main__':main()
