"""Worker processes for the --display app.

A Python thread of the app shares one interpreter lock with the window, so a long
calculation on a thread still holds the window. `call` runs a function in a worker
process that the app keeps instead. Each lane ("colour", "merge") is one process,
which does one call at a time. The process keeps its module state between calls,
so a module can keep data that it prepared for a later call. The caller waits for
the result: call it from a worker thread. When the process cannot run, the function
runs in this process."""
import importlib
import pickle
import subprocess
import sys
import threading
import traceback

_lanes, _lanes_lock = {}, threading.Lock()


class WorkerError(Exception):
    """The function raised an exception in the worker process. The message holds its
    traceback."""


def _function(name):
    module, _sep, attr = name.partition(":")
    return getattr(importlib.import_module(module), attr)


def _read(stream, n):
    data = stream.read(n)
    if len(data) != n:
        raise EOFError("the worker process stopped")
    return data


def _send(stream, value):
    data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(len(data).to_bytes(8, "big") + data)
    stream.flush()


def _receive(stream):
    return pickle.loads(_read(stream, int.from_bytes(_read(stream, 8), "big")))


def serve():
    """The worker process: read (function name, arguments) from stdin and write
    (True, result) or (False, traceback) to stdout, until stdin closes."""
    while True:
        try:
            name, args = _receive(sys.stdin.buffer)
        except EOFError:
            return
        try:
            out = (True, _function(name)(*args))
        except Exception:
            out = (False, traceback.format_exc())
        _send(sys.stdout.buffer, out)


class _Lane:
    def __init__(self):
        self.lock, self.proc = threading.Lock(), None

    def call(self, name, args):
        with self.lock:
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self.proc = subprocess.Popen(
                        [sys.executable, "-c", "from pdxaudit.worker import serve; serve()"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                _send(self.proc.stdin, (name, args))
                ok, value = _receive(self.proc.stdout)
            except (OSError, EOFError, pickle.UnpicklingError):
                if self.proc is not None:
                    self.proc.kill()
                self.proc = None
                return _function(name)(*args)
        if not ok:
            raise WorkerError(value)
        return value


def call(lane, name, *args):
    """The result of the function `name` ("module:function") for `args`, worked out in
    the worker process of `lane`."""
    with _lanes_lock:
        worker = _lanes.setdefault(lane, _Lane())
    return worker.call(name, args)
