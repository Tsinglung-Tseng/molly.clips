#!/usr/bin/env python3
"""
Clippings Watcher Service

监控 Obsidian Clippings 目录，当有 .md 文件出现时，
使用 `claude -p /obs-note` 整理笔记后写入 Vault 根目录，
并删除 Clippings 中的原始文件。

PM2:
    pm2 start ecosystem.config.js
"""

import os
import sys
import time
import logging
import queue
import subprocess
import threading
import re
from pathlib import Path
from threading import Timer
from datetime import datetime

# --- 路径配置 ---
CLIPS_DIR  = Path(__file__).parent.resolve()

_vault = os.environ.get('MOLLY_VAULT_PATH')
if not _vault:
    sys.exit("Error: MOLLY_VAULT_PATH environment variable is not set.")
VAULT_PATH = Path(_vault)
WATCH_PATH = VAULT_PATH / 'Clippings'
LOG_PATH   = CLIPS_DIR / 'logs' / 'watcher.log'

CLAUDE_BIN       = os.environ.get('MOLLY_CLAUDE_BIN') or os.environ.get('CLAUDE_BIN')
if not CLAUDE_BIN:
    sys.exit("Error: MOLLY_CLAUDE_BIN (or CLAUDE_BIN) environment variable is not set.")
DEBOUNCE_SECONDS = float(os.environ.get('MOLLY_DEBOUNCE_SEC', '5.0'))

MAX_RETRIES        = 4
INITIAL_BACKOFF_S  = 300   # 5 minutes


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def setup_logging():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = '%(asctime)s [%(levelname)s] %(message)s'
    handlers = [
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)

log = logging.getLogger('clips-watcher')


# ---------------------------------------------------------------------------
# Rate Limit handling
# ---------------------------------------------------------------------------

class RateLimitError(Exception):
    def __init__(self, delay_seconds: int):
        self.delay_seconds = delay_seconds

def _parse_reset_delay(output: str) -> int:
    """找出重置时间并计算需要等待的秒数"""
    # 匹配 "resets 1pm" 或 "resets 1:00pm"
    match = re.search(r"resets\s+(\d+)(?::\d+)?\s*(am|pm)", output, re.IGNORECASE)
    if not match:
        return 300  # 默认等待 5 分钟
    
    hour = int(match.group(1))
    ampm = match.group(2).lower()
    
    if ampm == 'pm' and hour < 12:
        hour += 12
    elif ampm == 'am' and hour == 12:
        hour = 0
    
    now = datetime.now()
    try:
        reset_time = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        # 如果算出来的重置时间在过去（例如现在 1:05pm，提示 1pm 重置），
        # 则视为下一次重置（或者当前就是重置瞬间）。
        if reset_time <= now:
            # 这种情况通常发生在正好处于重置边缘。
            # 我们等待固定的短暂时间后再试。
            return 120  # 等待 2 分钟缓冲区
        
        delay = (reset_time - now).total_seconds()
        return int(delay) + 60 # 额外加 1 分钟缓冲确保完全通过
    except ValueError:
        return 300


# ---------------------------------------------------------------------------
# Claude Code 调用
# ---------------------------------------------------------------------------

def run_obs_note(file_path: Path) -> bool:
    """
    调用 claude -p /obs-note 整理笔记。
    Claude Code 会直接将笔记写入 Vault 根目录。
    返回 True 表示成功（退出码 0）。
    """
    content = file_path.read_text(encoding='utf-8')
    prompt = f"/obs-note 请整理以下内容：\n\n{content}"

    cmd = [
        CLAUDE_BIN,
        '--print',
        '--dangerously-skip-permissions',
        prompt,
    ]

    log.info(f"  -> invoking claude /obs-note for: {file_path.name}")

    result = subprocess.run(
        cmd,
        cwd=str(VAULT_PATH),     # 在 vault 根目录运行，CLAUDE.md / skills 均可见
        stdin=subprocess.DEVNULL,  # 明确关闭 stdin，避免 claude 等待输入
        capture_output=True,       # 捕获输出以检测频率限制
        text=True,
        timeout=900,               # 最多等 15 分钟
    )

    # 将输出透传到日志，以便用户查看进度
    if result.stdout:
        # 过滤掉一些过于琐碎的内容，或者直接打印
        for line in result.stdout.splitlines():
            if line.strip(): print(f"    {line}")
    if result.stderr:
        for line in result.stderr.splitlines():
            if line.strip(): print(f"    {line}", file=sys.stderr)

    if result.returncode != 0:
        output = (result.stdout or "") + (result.stderr or "")
        if "hit your limit" in output or "resets" in output:
            delay = _parse_reset_delay(output)
            raise RateLimitError(delay)

        log.error(f"  ✗ claude exited with code {result.returncode}")
        return False

    log.info(f"  ✓ claude /obs-note completed for: {file_path.name}")
    return True


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class ClippingsPipeline:
    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, name='clips-worker', daemon=True)
        self._worker.start()

    def _worker_loop(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            self._do_process(item)
            self._queue.task_done()

    def process_file(self, file_path: Path):
        self._queue.put(file_path)

    def _do_process(self, file_path: Path):
        if not file_path.exists():
            log.warning(f"  - file gone before processing: {file_path.name}")
            return

        print(f"MOLLY_STATUS: processing {file_path.name}", flush=True)
        retries = 0
        while True:
            try:
                success = run_obs_note(file_path)
                if success:
                    file_path.unlink(missing_ok=True)
                    log.info(f"  ✓ deleted original: {file_path.name}")
                break
            except RateLimitError as e:
                retries += 1
                if retries > MAX_RETRIES:
                    log.error(f"  ✗ gave up after {MAX_RETRIES} retries: {file_path.name}")
                    break
                backoff = INITIAL_BACKOFF_S * (2 ** (retries - 1))
                log.warning(f"  ⚠ Rate limit hit. Retry {retries}/{MAX_RETRIES} in {backoff}s...")
                time.sleep(backoff)
                log.info(f"  ↻ Retrying: {file_path.name}")
                continue
            except subprocess.TimeoutExpired:
                print(f"MOLLY_STATUS: error timeout: {file_path.name}", flush=True)
                log.error(f"  ✗ timeout processing: {file_path.name}")
                break
            except Exception as e:
                print(f"MOLLY_STATUS: error {e}", flush=True)
                log.error(f"  ✗ error [{file_path.name}]: {e}", exc_info=True)
                break
        print("MOLLY_STATUS: idle", flush=True)

    def close(self):
        self._queue.put(None)
        self._worker.join(timeout=10)


# ---------------------------------------------------------------------------
# File system event handler
# ---------------------------------------------------------------------------

class ClippingsHandler:
    def __init__(self, pipeline: ClippingsPipeline):
        self.pipeline = pipeline
        self._timers: dict[str, Timer] = {}

    def on_change(self, path: str):
        p = Path(path)
        if p.suffix != '.md':
            return
        if p.parent != WATCH_PATH:
            return
        log.info(f"[detected] {p.name}")
        self._debounce(path)

    def _debounce(self, path: str):
        if path in self._timers:
            self._timers[path].cancel()
        t = Timer(DEBOUNCE_SECONDS, self._run, args=[path])
        t.daemon = True
        t.start()
        self._timers[path] = t

    def _run(self, path: str):
        self._timers.pop(path, None)
        self.pipeline.process_file(Path(path))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    setup_logging()

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileSystemEvent
    except ImportError:
        print("缺少依赖，请运行: uv add watchdog")
        sys.exit(1)

    log.info("=" * 60)
    log.info("Clippings Watcher (obs-note via claude -p)")
    log.info(f"  Watch    : {WATCH_PATH}")
    log.info(f"  Vault    : {VAULT_PATH}")
    log.info(f"  Claude   : {CLAUDE_BIN}")
    log.info(f"  Debounce : {DEBOUNCE_SECONDS}s")
    log.info("=" * 60)

    if not Path(CLAUDE_BIN).exists():
        log.error(f"claude not found at: {CLAUDE_BIN}")
        sys.exit(1)

    pipeline = ClippingsPipeline()
    handler_logic = ClippingsHandler(pipeline)

    class WatchdogBridge(FileSystemEventHandler):
        def on_created(self, event: FileSystemEvent):
            if not event.is_directory:
                handler_logic.on_change(event.src_path)

        def on_modified(self, event: FileSystemEvent):
            if not event.is_directory:
                handler_logic.on_change(event.src_path)

    observer = Observer()
    observer.schedule(WatchdogBridge(), str(WATCH_PATH), recursive=False)
    observer.start()

    log.info("Watching for clippings... (Ctrl+C to stop)")
    print("MOLLY_READY", flush=True)

    try:
        while observer.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down...")
        observer.stop()
        observer.join()
        pipeline.close()
        log.info("Watcher stopped.")


if __name__ == '__main__':
    main()
