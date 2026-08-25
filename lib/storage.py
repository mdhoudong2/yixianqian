"""共享 JSON 文件存储：跨进程原子写 + 文件锁。

被 bot（多线程轮询）与 H5 后端（gunicorn 多 worker）同时读写时保证安全：

- `load_json` / `save_json`：save 先写临时文件再 `os.replace` 原子替换，
  读进程永远不会看到写了一半的文件（旧实现直接 open('w') 有损坏风险）。
- `update_json`：读-改-写整体加锁 —— 进程内用 threading.Lock，
  进程间用 flock（Linux/macOS 均可用），避免并发覆盖去重记录。
"""
import json
import os
import tempfile
import threading

try:
    import fcntl
except ImportError:  # Windows 等无 flock 的平台退化为仅线程锁
    fcntl = None

_thread_locks = {}
_locks_guard = threading.Lock()


def _thread_lock(path):
    with _locks_guard:
        if path not in _thread_locks:
            _thread_locks[path] = threading.Lock()
        return _thread_locks[path]


class _FileLock:
    """基于 flock 的跨进程文件锁（锁文件与数据文件分离，避免 rename 换掉锁文件）"""

    def __init__(self, path):
        self._f = open(path, "a+")

    def __enter__(self):
        if fcntl:
            fcntl.flock(self._f.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if fcntl:
            fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        self._f.close()


def load_json(path, default=None):
    """读取 JSON，文件不存在或损坏时返回 default（不抛异常）"""
    path = os.fspath(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    """原子写 JSON：先写同目录临时文件，fsync 后 rename 覆盖"""
    path = os.fspath(path)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".yx-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_json(path, default, mutator):
    """加锁读-改-写：mutator(data) 返回新数据；返回 None 表示放弃写入（保持原样）。

    返回：写入后的数据（放弃写入时返回原数据）。
    """
    path = os.fspath(path)
    with _thread_lock(path), _FileLock(path + ".lock"):
        data = load_json(path, default)
        new_data = mutator(data)
        if new_data is not None:
            save_json(path, new_data)
            return new_data
        return data
