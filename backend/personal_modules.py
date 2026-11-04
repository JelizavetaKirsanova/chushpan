"""Composition of new modules inside the existing voice/desktop lifecycle."""

from backend.analytics import Analytics
from backend.mac_center import MacCenter
from backend.memory.library import MemoryLibrary
from backend.sync.service import DeviceSync


class PersonalModules:
    def __init__(self, settings, store, tools, bus):
        self.memory = MemoryLibrary(store, settings, bus)
        self.mac = MacCenter(settings, store, tools, bus)
        self.sync = DeviceSync(settings, store, tools, bus)
        self.analytics = Analytics(settings, store, tools, bus)

    async def close(self):
        for service in (self.sync, self.mac, self.memory, self.analytics):
            await service.close()
