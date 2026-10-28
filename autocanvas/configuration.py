"""Settings persistence and application timing, outside processing modules."""
from .config import Settings
from .outputs import atomic_json

RESTART = {'root', 'host', 'port'}
IDLE = {'asr_backend', 'asr_dtype', 'asr_idle_seconds', 'model', 'device', 'chunk_seconds', 'sample_every', 'live_queue_chunks', 'keywords', 'keyword_debounce'}
LABELS = {
    'root': ('运行数据目录', '服务'), 'host': ('监听地址', '服务'), 'port': ('监听端口', '服务'),
    'course_ids': ('同步课程白名单（留空表示全部）', '自动化'),
    'auto_asr': ('自动转写回放', '自动化'), 'auto_slides': ('自动抽取 Slides', '自动化'), 'auto_live': ('自动监听直播', '自动化'),
    'course_interval': ('课表同步间隔 / 秒', '自动化'), 'sync_interval': ('视频同步间隔 / 秒', '自动化'),
    'assignment_interval': ('作业同步间隔 / 秒', '自动化'),
    'schedule_interval': ('调度检查间隔 / 秒', '自动化'), 'live_lead_seconds': ('直播提前启动 / 秒', '直播'),
    'live_queue_chunks': ('直播音频队列容量 / 块', '直播'), 'model': ('本地模型名称或路径', '转写'),
    'asr_backend': ('转写后端（torch/mlx）', '转写'),
    'asr_dtype': ('模型精度', '转写'), 'asr_idle_seconds': ('空闲卸载秒数', '转写'),
    'device': ('推理设备', '转写'), 'chunk_seconds': ('音频块长度 / 秒', '转写'), 'sample_every': ('画面采样间隔 / 秒', 'Slides'),
    'keywords': ('监听关键词', '直播'), 'keyword_debounce': ('关键词防抖间隔 / 秒', '直播'),
}


class Configuration:
    def __init__(self, settings, busy, apply_idle, apply_hot):
        self.settings, self.busy = settings.validate(), busy
        self.apply_idle, self.apply_hot = apply_idle, apply_hot
        self.path = getattr(settings, '_settings_path', settings.root/'settings.json')
        self.desired = settings.public()
        self.revision = 0

    def snapshot(self):
        current = self.settings.public()
        pending = [k for k in current if current[k] != self.desired[k]]
        schema = []
        for key, (label, group) in LABELS.items():
            value = self.desired[key]
            kind = 'boolean' if type(value) is bool else 'number' if type(value) in (int, float) else 'list' if isinstance(value, list) else 'text'
            schema.append({'key': key, 'label': label, 'group': group, 'type': kind,
                           'apply': 'restart' if key in RESTART else 'idle' if key in IDLE else 'immediate'})
        return {'current': current, 'desired': self.desired, 'schema': schema, 'revision': self.revision,
                'restart_required': [k for k in pending if k in RESTART], 'waiting_for_idle': [k for k in pending if k in IDLE]}

    def save(self, patch, revision):
        if revision != self.revision:
            raise ValueError('设置已被其他页面修改，请刷新后重试')
        if not isinstance(patch, dict) or set(patch)-set(LABELS):
            raise ValueError('存在未知配置项')
        checked = Settings(**{**self.desired, **patch}).validate().public()
        atomic_json(self.path, checked)
        self.desired = checked
        for key, value in checked.items():
            if key not in RESTART | IDLE:
                setattr(self.settings, key, value)
        self.apply_hot()
        self.revision += 1
        self.apply_ready()
        return self.snapshot()

    def apply_ready(self):
        if self.busy():
            return
        changed = {k for k in IDLE if getattr(self.settings, k) != self.desired[k]}
        if changed:
            self.apply_idle(self.desired, changed)
            for key in IDLE:
                setattr(self.settings, key, self.desired[key])
