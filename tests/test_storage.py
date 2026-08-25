"""lib/storage.py 单测：原子写、损坏容错、并发读-改-写（线程 + 多进程）。"""
import json
import multiprocessing
import threading

from lib import storage


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "data.json"
    storage.save_json(p, {"a": 1, "b": [1, 2, 3]})
    assert storage.load_json(p) == {"a": 1, "b": [1, 2, 3]}
    # 原子写不残留临时文件
    assert [f.name for f in tmp_path.iterdir()] == ["data.json"]


def test_load_missing_returns_default(tmp_path):
    assert storage.load_json(tmp_path / "nope.json", {"d": 1}) == {"d": 1}
    assert storage.load_json(tmp_path / "nope.json") is None


def test_load_corrupt_returns_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{broken", encoding="utf-8")
    assert storage.load_json(p, []) == []


def test_update_json_mutator_none_keeps_original(tmp_path):
    p = tmp_path / "d.json"
    storage.save_json(p, {"v": 1})
    out = storage.update_json(p, {}, lambda data: None)
    assert out == {"v": 1}
    assert storage.load_json(p) == {"v": 1}


def test_update_json_threads_no_lost_updates(tmp_path):
    p = tmp_path / "c.json"
    n_threads, n_each = 8, 50

    def worker(worker_id):
        for i in range(n_each):
            storage.update_json(p, [], lambda data, w=worker_id, n=i: data + [f"{w}-{n}"])

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = storage.load_json(p)
    assert len(data) == n_threads * n_each
    assert len(set(data)) == n_threads * n_each


def _proc_worker(p, worker_id, n_each):
    for i in range(n_each):
        storage.update_json(p, [], lambda data, w=worker_id, n=i: data + [f"{w}-{n}"])


def test_update_json_processes_no_lost_updates(tmp_path):
    p = str(tmp_path / "cp.json")
    n_procs, n_each = 4, 50
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_proc_worker, args=(p, w, n_each)) for w in range(n_procs)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join()
        assert pr.exitcode == 0
    data = storage.load_json(p)
    assert len(data) == n_procs * n_each
    assert len(set(data)) == n_procs * n_each


def test_concurrent_read_never_sees_partial(tmp_path):
    p = str(tmp_path / "rw.json")
    storage.save_json(p, {"key": "x" * 2000})
    stop = threading.Event()
    errors = []

    def writer():
        i = 0
        while not stop.is_set():
            storage.save_json(p, {"key": "v" * (1000 + i % 1000)})
            i += 1

    def reader():
        while not stop.is_set():
            data = storage.load_json(p)
            if not isinstance(data, dict):
                errors.append("non-dict: %r" % data)
            else:
                json.dumps(data)

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    import time
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join()
    assert not errors
