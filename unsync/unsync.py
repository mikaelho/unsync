import asyncio
import concurrent
import functools
import inspect
import threading
import os
from threading import Thread
from typing import Generic, TypeVar


class unsync(object):
    thread_executor = concurrent.futures.ThreadPoolExecutor()
    process_executor = None
    loop = asyncio.new_event_loop()
    thread = None
    unsync_functions = {}

    @staticmethod
    def thread_target(loop):
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def __init__(self, *args, **kwargs):
        self.args = []
        self.kwargs = {}
        if len(args) == 1 and inspect.isfunction(args[0]):
            self._set_func(args[0])
        else:
            self.args = args
            self.kwargs = kwargs
            self.func = None

    @property
    def cpu_bound(self):
        return 'cpu_bound' in self.kwargs and self.kwargs['cpu_bound']

    def _set_func(self, func):
        assert inspect.isfunction(func)
        self.func = func
        functools.update_wrapper(self, func)
        unsync.unsync_functions[(func.__module__, func.__name__)] = func

    def __call__(self, *args, **kwargs):
        if self.func is None:
            self._set_func(args[0])
            return self
        if inspect.iscoroutinefunction(self.func):
            if self.cpu_bound:
                raise TypeError('The CPU bound unsync function %s may not be async or a coroutine' % self.func.__name__)
            future = self.func(*args, **kwargs)
        else:
            if self.cpu_bound:
                if unsync.process_executor is None:
                    unsync.process_executor = concurrent.futures.ProcessPoolExecutor()
                future = unsync.process_executor.submit(
                    _multiprocess_target, (self.func.__module__, self.func.__name__), *args, **kwargs)
            else:
                future = unsync.thread_executor.submit(self.func, *args, **kwargs)
        return Unfuture(future)

    def __get__(self, instance, owner):
        def _call(*args, **kwargs):
            return self(instance, *args, **kwargs)

        functools.update_wrapper(_call, self.func)
        return _call


def _multiprocess_target(func_name, *args, **kwargs):
    # On Windows MP turns the main module into __mp_main__ in multiprocess targets
    if os.name == 'nt' and func_name[0] == '__main__':
        func_name = ('__mp_main__', func_name[1])
    __import__(func_name[0])
    return unsync.unsync_functions[func_name](*args, **kwargs)


T = TypeVar('T')

class Unfuture(Generic[T]):
    @staticmethod
    def from_value(value):
        future = Unfuture()
        future.set_result(value)
        return future

    def __init__(self, future=None):
        def callback(source, target):
            try:
                asyncio.futures._chain_future(source, target)
            except Exception as exc:
                if self.concurrent_future.set_running_or_notify_cancel():
                    self.concurrent_future.set_exception(exc)
                raise

        if asyncio.iscoroutine(future):
            future = asyncio.ensure_future(future, loop=unsync.loop)
        if isinstance(future, concurrent.futures.Future):
            self.concurrent_future = future
            self.future = asyncio.Future(loop=unsync.loop)
            self.future._loop.call_soon_threadsafe(callback, self.concurrent_future, self.future)
        else:
            self.future = future or asyncio.Future(loop=unsync.loop)
            self.concurrent_future = concurrent.futures.Future()
            self.future._loop.call_soon_threadsafe(callback, self.future, self.concurrent_future)

    def __iter__(self):
        return self.future.__iter__()

    __await__ = __iter__

    def result(self, *args, **kwargs) -> T:
        # The asyncio Future may have completed before the concurrent one
        if self.future.done():
            return self.future.result()
        # Don't allow waiting in the unsync.thread loop since it will deadlock
        if threading.current_thread() == unsync.thread and not self.concurrent_future.done():
            raise asyncio.InvalidStateError
        # Wait on the concurrent Future outside unsync.thread
        return self.concurrent_future.result(*args, **kwargs)

    def done(self):
        return self.future.done() or self.concurrent_future.done()

    def set_result(self, value):
        return self.future._loop.call_soon_threadsafe(lambda: self.future.set_result(value))

    @unsync
    async def then(self, continuation):
        await self
        result = continuation(self)
        if hasattr(result, '__await__'):
            return await result
        return result


unsync.thread = Thread(target=unsync.thread_target, args=(unsync.loop,), daemon=True)
unsync.thread.start()
