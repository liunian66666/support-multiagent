"""Patch a public main symbol and its extracted auth owner with one shared fake.

Legacy contract tests call through ``main`` while FastAPI dependencies now execute
in ``business.admin.auth``. Sharing the replacement preserves each test's
query/call assertions during the module split.
"""

from unittest.mock import patch

from business.admin import auth as auth_module
from business.admin import catalog as catalog_module
from business.admin import sessions as sessions_module
from business.admin import tickets as tickets_module
from business.admin import ops as ops_module
from business.admin import rbac as rbac_module
from business.admin import platform as platform_module
from business.admin import content as content_module
from business.agent import internal as internal_module
from business.chat import realtime as realtime_module
from business import server as server_module


class _BusinessPatch:
    def __init__(self, target, name, *args, **kwargs):
        self.target = target
        self.name = name
        self.original = patch.object(target, name, *args, **kwargs)
        self.owners = []
        self.started = False

    def start(self):
        result = self.original.start()
        self.started = True
        if self.target.__name__ == "main_compat":
            for module in (auth_module, catalog_module, sessions_module,
                           tickets_module, ops_module, rbac_module,
                           platform_module, content_module, internal_module,
                           realtime_module, server_module):
                if hasattr(module, self.name):
                    owner = patch.object(module, self.name,
                                         new=getattr(self.target, self.name))
                    owner.start()
                    self.owners.append(owner)
        return result

    def stop(self):
        for owner in reversed(self.owners):
            owner.stop()
        self.owners.clear()
        if self.started:
            self.original.stop()
            self.started = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()
        return False


def patch_business(target, name, *args, **kwargs):
    return _BusinessPatch(target, name, *args, **kwargs)
