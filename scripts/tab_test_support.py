"""Isolated workspace stores, including Windows restricted-token test runs.

Default temp paths can be blocked; Python 3.13 also uses a private Windows
ACL that can deny its own sandbox token access. Inherited workspace
permissions work, and cleanup closes the isolated SQLite store first.
"""
import atexit
import os
import shutil
import sys
import time
import uuid
from pathlib import Path


class TemporaryDirectory:
    def __init__(self, prefix=".tab-test-", dir=None, ignore_cleanup_errors=False):
        if not prefix.startswith(".tab-test-"):
            prefix = ".tab-test-" + prefix
        parent = Path(dir) if dir else Path(__file__).resolve().parent
        self.name = str(parent / (prefix + uuid.uuid4().hex))
        os.mkdir(self.name)
        atexit.register(self.cleanup)

    def cleanup(self):
        if os.path.exists(self.name):
            store = sys.modules.get("core.session_manager_store")
            if store and os.path.commonpath((self.name, store.DB_FILE)) == self.name:
                store.close()  # Windows cannot remove an open SQLite store
            for attempt in range(20):
                try:
                    shutil.rmtree(self.name)
                    break
                except PermissionError:
                    # A just-abandoned policy read may still have a Windows
                    # file handle open. Retry briefly, then fail loudly.
                    if attempt == 19:
                        raise
                    time.sleep(.025)
        atexit.unregister(self.cleanup)

    def __enter__(self):
        return self.name

    def __exit__(self, *args):
        self.cleanup()
