"""Compatibility entry point for the shared permission center."""

from backend.permission_center import PermissionCenter


async def report(settings, center=None):
    return await (center or PermissionCenter(settings)).report()
