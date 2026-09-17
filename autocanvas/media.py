"""ffmpeg/ffprobe transport, with bounded reads and deterministic process cleanup."""
import asyncio
import json
from pathlib import Path
from .types import AudioChunk, MediaError, MediaSource


def inputs(source):
    args = []
    if source.location.startswith(('http://', 'https://')):
        args += ['-rw_timeout', '20000000']
        if source.headers:
            if any('\r' in k+v or '\n' in k+v for k, v in source.headers.items()):
                raise ValueError('Invalid media headers')
            args += ['-headers', ''.join(f'{k}: {v}\r\n' for k, v in source.headers.items())]
    return args


async def stop(process):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def command(args, timeout=60):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
        if proc.returncode:
            raise MediaError(f'Media process exited {proc.returncode}')
        return stdout
    finally:
        await stop(proc)


async def probe(source):
    raw = await command(['ffprobe', '-v', 'error', *inputs(source), '-show_streams', '-show_format', '-of', 'json', source.location])
    return json.loads(raw)


async def audio(source, *, sample_rate=16000, chunk_seconds=3, offset=0, duration=None, realtime=False):
    size = int(sample_rate * chunk_seconds) * 2
    if size <= 0:
        raise ValueError('Audio chunk size must be positive')
    args = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', *inputs(source)]
    if realtime:
        args += ['-re']
    if offset:
        args += ['-ss', str(offset)]
    args += ['-i', source.location]
    if duration is not None:
        args += ['-t', str(duration)]
    args += ['-vn', '-ac', '1', '-ar', str(sample_rate), '-f', 's16le', 'pipe:1']
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    position = offset
    try:
        while True:
            try:
                block = await asyncio.wait_for(proc.stdout.readexactly(size), max(30, chunk_seconds * 3))
            except asyncio.IncompleteReadError as exc:
                block = exc.partial
            if not block:
                break
            block = block[:len(block) // 2 * 2]
            if block:
                chunk = AudioChunk(block, sample_rate, position)
                yield chunk
                position += chunk.duration
            if len(block) < size:
                break
        await asyncio.wait_for(proc.wait(), 10)
        if proc.returncode:
            raise MediaError(f'Audio reader exited {proc.returncode}')
    finally:
        await stop(proc)


async def sample_frames(source, destination: Path, *, every=5, duration=None):
    if every <= 0:
        raise ValueError('Frame interval must be positive')
    destination.mkdir(parents=True, exist_ok=True)
    args = ['ffmpeg', '-nostdin', '-y', '-hide_banner', '-loglevel', 'error', *inputs(source), '-i', source.location]
    if duration is not None:
        args += ['-t', str(duration)]
    args += ['-an', '-vf', f'fps=1/{every}:start_time=0', '-q:v', '2', str(destination/'%08d.jpg')]
    await command(args, timeout=7200)
    if not next(destination.glob('*.jpg'), None):
        raise MediaError('No video frames decoded')


async def select(sources, purpose, view=None):
    if view is not None:
        matches = [s for s in sources if s.view == str(view)]
        if not matches:
            raise MediaError('Requested view unavailable')
        return matches[0]
    candidates = []
    for source in sources:
        try:
            if purpose == 'audio':
                import array
                reader = audio(source, duration=3)
                try:
                    block = await anext(reader)
                finally:
                    await reader.aclose()
                samples = array.array('h', block.pcm)
                score = max((abs(s) for s in samples), default=0)
            else:
                info = await probe(source)
                stream = next(s for s in info['streams'] if s.get('codec_type') == 'video')
                score = (int(stream['width']) * int(stream['height']), -int(stream.get('bit_rate') or info.get('format', {}).get('bit_rate') or 10**12))
            candidates.append((score, source))
        except (MediaError, StopAsyncIteration, KeyError, ValueError, StopIteration, asyncio.TimeoutError):
            continue
    if not candidates:
        raise MediaError('No readable media source')
    return max(candidates, key=lambda x: x[0])[1]
