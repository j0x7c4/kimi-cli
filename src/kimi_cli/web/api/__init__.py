"""API routes."""

from kimi_cli.web.api import admin as admin_module
from kimi_cli.web.api import auth_user, config, memory, open_in, sessions
from kimi_cli.web.api import branding as branding_module

config_router = config.router
sessions_router = sessions.router
work_dirs_router = sessions.work_dirs_router
open_in_router = open_in.router
auth_router = auth_user.router
admin_router = admin_module.router
branding_public_router = branding_module.public_router
branding_admin_router = branding_module.admin_router
memory_router = memory.router

__all__ = [
    "admin_router",
    "auth_router",
    "branding_admin_router",
    "branding_public_router",
    "config_router",
    "memory_router",
    "open_in_router",
    "sessions_router",
    "work_dirs_router",
]
