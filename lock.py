from __future__ import annotations

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DOMAIN
from .entity import HondaLinkEntity
from .util import all_door_locks_locked, status_body


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    async_add_entities([HondaLinkDoorLock(coordinator, entry)])


class HondaLinkDoorLock(HondaLinkEntity, LockEntity):
    _attr_translation_key = "doors"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "doors_lock")

    @property
    def is_locked(self) -> bool | None:
        return all_door_locks_locked(status_body(self.coordinator.data or {}))

    async def async_lock(self, **kwargs) -> None:
        await self.coordinator.async_lock()

    async def async_unlock(self, **kwargs) -> None:
        await self.coordinator.async_unlock()
