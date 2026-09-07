"""API routes."""

from kimi_cli.web.api import admin as admin_module
from kimi_cli.web.api import (
    auth_user,
    capabilities,
    config,
    memory,
    open_in,
    sandbox_assets,
    sessions,
    warmpool,
)
from kimi_cli.web.api import branding as branding_module

config_router = config.router
sessions_router = sessions.router
work_dirs_router = sessions.work_dirs_router
agents_router = sessions.agents_router
open_in_router = open_in.router
auth_router = auth_user.router
admin_router = admin_module.router
branding_public_router = branding_module.public_router
branding_admin_router = branding_module.admin_router
memory_router = memory.router
capabilities_router = capabilities.capabilities_router
sandbox_assets_router = sandbox_assets.router
warmpool_router = warmpool.router

__all__ = [
    "admin_router",
    "agents_router",
    "auth_router",
    "branding_admin_router",
    "branding_public_router",
    "capabilities_router",
    "config_router",
    "memory_router",
    "open_in_router",
    "sandbox_assets_router",
    "sessions_router",
    "warmpool_router",
    "work_dirs_router",
]
