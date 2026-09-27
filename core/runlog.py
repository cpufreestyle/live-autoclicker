"""运行日志落盘：扫描线程的命中/点击/异常和界面日志都写进同一份 run.log。

所有日志都在扫描线程里产生，所以这里用「队列 + 单个写线程」：写文件本身有锁和 IO，
放在扫描线程里会让单帧耗时平白多几毫秒，还可能和界面线程抢同一个文件句柄。
写失败的唯一后果是看不到日志，绝不能让程序崩。
"""
from __future__ import annotations

import os
import queue
import threading
import time

# 单条日志上限：异常栈偶尔会带上整帧 numpy 的 repr，不能让它把日志撑爆
_MAX_MESSAGE = 2000
_LOG_NAME = "run.log"
_FLUSH_AT = 64          # 攒够这么多条就落一次盘
_IDLE_FLUSH = 0.3       # 空闲这么久也落一次，保证 stop() 时尾巴不长


class RunLogger:
    """把日志追加写到 <base_dir>/logs/run.log；队列满就丢，绝不阻塞扫描线程。"""

    def __init__(self, base_dir: str, enabled: bool = True) -> None:
        self.path = os.path.join(base_dir, "logs", _LOG_NAME)
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=4096)
        self._worker: threading.Thread | None = None
        self._enabled = bool(enabled)
        self._lock = threading.Lock()
        self.lost = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            if self._worker is not None:
                return
            worker = threading.Thread(target=self._drain, name="run-log", daemon=True)
            worker.start()
            self._worker = worker

    def write(self, level: str, message: str) -> None:
        if not self._enabled:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        text = str(message).replace("\r", " ").replace("\n", " | ")
        if len(text) > _MAX_MESSAGE:
            text = text[:_MAX_MESSAGE] + " …(截断)"
        line = f"{stamp} [{level}] {text}\n"
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            # 队列满 = 磁盘跟不上，丢一条日志也不能堵住扫描线程
            self.lost += 1

    def stop(self, timeout: float = 1.5) -> None:
        with self._lock:
            worker = self._worker
            self._worker = None
        if worker is None:
            return
        # None 是收尾哨兵：写完队列里剩下的再退出
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # 队列被日志塞满时先把旧的倒掉，保证哨兵进得去
            try:
                while True:
                    self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                return
        if worker is not threading.current_thread():
            worker.join(timeout=timeout)

    def close(self) -> None:
        self.stop()

    # ---------- 内部 ----------
    def _drain(self) -> None:
        pending: list[str] = []
        last_write = time.monotonic()
        while True:
            timeout = 0.2 if not pending else max(0.0, _IDLE_FLUSH - (time.monotonic() - last_write))
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                if pending and time.monotonic() - last_write >= _IDLE_FLUSH:
                    self._flush(pending)
                    last_write = time.monotonic()
                continue
            if item is None:
                break
            pending.append(item)
            if len(pending) >= _FLUSH_AT:
                self._flush(pending)
                last_write = time.monotonic()
        if pending:
            self._flush(pending)

    def _flush(self, pending: list[str]) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write("".join(pending))
                fh.flush()
        except OSError:
            pass      # 磁盘满/被占用：日志只是排障工具，不能因此中断扫描
        finally:
            pending.clear()
