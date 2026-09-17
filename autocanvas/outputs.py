"""Application output utilities; paths never include remote titles or credentials."""
import json
import os
from pathlib import Path
from dataclasses import asdict


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temp.replace(path)


class Transcript:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder
        self.path = folder/'segments.jsonl'
        self.end = 0.0
        if self.path.exists():
            valid = []
            for line in self.path.read_text().splitlines():
                try:
                    item = json.loads(line)
                    self.end = max(self.end, float(item['end']))
                    valid.append(json.dumps(item, ensure_ascii=False))
                except (ValueError, KeyError):
                    break
            self.path.write_text(''.join(line+'\n' for line in valid))

    def append(self, segment):
        if segment.end <= self.end:
            return
        with self.path.open('a') as handle:
            handle.write(json.dumps(asdict(segment), ensure_ascii=False)+'\n')
            handle.flush()
            os.fsync(handle.fileno())
        self.end = segment.end

    def finish(self):
        rows = [json.loads(s) for s in self.path.read_text().splitlines()] if self.path.exists() else []
        atomic_json(self.folder/'transcript.json', rows)
        tmp = self.folder/'transcript.txt.tmp'
        tmp.write_text(''.join(f"[{int(r['start'])//3600:02}:{int(r['start'])//60%60:02}:{int(r['start'])%60:02}] {r['text']}\n" for r in rows if r['text']))
        tmp.replace(self.folder/'transcript.txt')
        return self.folder/'transcript.json'


def append_event(path, event):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        handle.write(json.dumps(event, ensure_ascii=False)+'\n')
