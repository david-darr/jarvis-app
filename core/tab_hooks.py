"""Declared folder-tab hooks. Failures and slow consumers never stop the app."""
import asyncio
import copy
import contextvars
import importlib
import inspect
import logging
import threading

from core import tab_folders

logger = logging.getLogger(__name__)
TIMEOUT = 2
_active = {}
_retired = []
_tasks = set()
_lock = None
_lock_loop = None
_thread_slots = threading.BoundedSemaphore(8)


def _track(task, *, report=True):
    _tasks.add(task)
    def finished(done):
        _tasks.discard(done)
        if not done.cancelled():
            try:
                done.result()
            except Exception:
                if report:
                    logger.exception("tab hook task failed")
    task.add_done_callback(finished)
    return task


async def _in_thread(fn, *args):
    # Abandoned sync calls cannot occupy the app's shared executor or keep
    # Python alive at exit. Bound them so repeated messages cannot leak threads.
    if not _thread_slots.acquire(blocking=False):
        raise RuntimeError("Tab hook workers are busy; skipped")
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    context = contextvars.copy_context()
    def finish(result, error):
        if not future.done():
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(result)
    def run():
        result, error = None, None
        try:
            result = context.run(fn, *args)
        except BaseException as exc:
            error = exc
        finally:
            _thread_slots.release()
        try:
            loop.call_soon_threadsafe(finish, result, error)
        except RuntimeError:
            pass  # the caller's loop closed after abandonment
    try:
        threading.Thread(target=run, name="tab-hook", daemon=True).start()
    except BaseException:
        _thread_slots.release()
        raise
    return await future


async def _invoke(slug, name, fn, *args):
    async def run():
        try:
            if inspect.iscoroutinefunction(fn):
                return await fn(*args)
            result = await _in_thread(fn, *args)
            return await result if inspect.isawaitable(result) else result
        except asyncio.CancelledError:
            raise
        except BaseException:
            logger.exception("tab %s hook %s failed; skipped", slug, name)
            return None
    task = _track(asyncio.create_task(run()), report=False)
    try:
        done, _ = await asyncio.wait({task}, timeout=TIMEOUT)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if not done:
        task.cancel()
        logger.warning("tab %s hook %s exceeded %s seconds; abandoned", slug, name, TIMEOUT)
        return None
    try:
        return task.result()
    except asyncio.CancelledError:
        return None
    except Exception:
        logger.exception("tab %s hook %s failed; skipped", slug, name)
        return None


def _module(entry):
    if not tab_folders._allowed(entry):
        return None
    return importlib.import_module(f"kairos_tabs.{entry['slug']}.hooks")


async def _declared(entry, name, *args):
    if name not in entry["_metadata"].get("hooks", []):
        return None
    async def call():
        mod = await _in_thread(_module, entry)
        if mod is None:
            return None
        fn = getattr(mod, name)
        if inspect.iscoroutinefunction(fn):
            return await fn(*args)
        result = await _in_thread(fn, *args)
        return await result if inspect.isawaitable(result) else result
    return await _invoke(entry["slug"], name, call)


def retire(slug):
    """Keep the old stop function before its package is unregistered."""
    old = _active.pop(slug, None)
    if old:
        _retired.append((slug, old))
    from core.tab_api import unregister_sync
    unregister_sync(slug)
    request_refresh()


def request_refresh():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # app startup reconciles synchronous discovery
    _track(loop.create_task(reconcile()))


async def reconcile():
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    async with _lock:
        for slug, (entry, mod) in list(_active.items()):
            if (tab_folders._registered.get(slug) is not entry
                    or not await _invoke(slug, "policy", tab_folders._allowed, entry)):
                _active.pop(slug, None)
                _retired.append((slug, (entry, mod)))
                from core.tab_api import unregister_sync
                unregister_sync(slug)
        while _retired:
            slug, (entry, mod) = _retired.pop(0)
            if "stop" in entry["_metadata"].get("hooks", []):
                await _invoke(slug, "stop", getattr(mod, "stop", None))
        for slug, entry in list(tab_folders._registered.items()):
            if slug in _active or not entry["_metadata"].get("hooks"):
                continue
            # Loading and policy checks have the same deadline as a hook.
            mod = await _invoke(slug, "load", lambda: _module(entry))
            if mod is None:
                continue
            _active[slug] = (entry, mod)
            if "start" in entry["_metadata"]["hooks"]:
                await _invoke(slug, "start", getattr(mod, "start", None))


async def stop_all():
    for task in list(_tasks):
        task.cancel()
    for slug in list(_active):
        old = _active.pop(slug)
        _retired.append((slug, old))
        from core.tab_api import unregister_sync
        unregister_sync(slug)
    while _retired:
        slug, (entry, mod) = _retired.pop(0)
        if "stop" in entry["_metadata"].get("hooks", []):
            await _invoke(slug, "stop", getattr(mod, "stop", None))


def emit_message(source, message):
    """Schedule passive consumers; do no imports, scans or hook work here."""
    async def dispatch():
        await asyncio.gather(*(_declared(entry, "on_message", copy.deepcopy(source), copy.deepcopy(message))
                               for entry in list(tab_folders._registered.values())))
    try:
        _track(asyncio.get_running_loop().create_task(dispatch()))
    except Exception:
        logger.exception("tab message dispatch could not be scheduled")


async def calendar_items(user, start, end):
    results = await asyncio.gather(*(_declared(entry, "calendar_items", user, start, end)
                                    for entry in list(tab_folders._registered.values())))
    items = []
    for result in results:
        if isinstance(result, list):
            for item in result:
                if (isinstance(item, dict) and item.get("source") == "tab"
                        and isinstance(item.get("source_label"), str)
                        and isinstance(item.get("toggle_url"), str)
                        and item["toggle_url"].startswith("/api/") and not "\\" in item["toggle_url"]):
                    items.append(item)
    return items
