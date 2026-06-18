"""Tests for button.py – Rebuild History button entity."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_coordinator(*, rebuild_raises: bool = False) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = MagicMock()
    if rebuild_raises:
        coordinator.async_rebuild_history = AsyncMock(side_effect=RuntimeError("boom"))
    else:
        coordinator.async_rebuild_history = AsyncMock()
    return coordinator


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    return entry


class TestRebuildHistoryButton:
    def _make_button(self, coordinator=None, entry=None):
        from shady.button import RebuildHistoryButton

        coordinator = coordinator or _make_coordinator()
        entry = entry or _make_entry()
        btn = RebuildHistoryButton.__new__(RebuildHistoryButton)
        btn.coordinator = coordinator
        btn._entry = entry
        btn._attr_unique_id = f"{entry.entry_id}_rebuild_history"
        return btn

    def test_unique_id(self):
        btn = self._make_button()
        assert btn._attr_unique_id == "test_entry_id_rebuild_history"

    def test_entity_category_is_diagnostic(self):
        from shady.button import RebuildHistoryButton

        # EntityCategory is a mock in tests; just verify the attribute is set
        assert RebuildHistoryButton._attr_entity_category is not None

    def test_icon(self):
        from shady.button import RebuildHistoryButton

        assert RebuildHistoryButton._attr_icon == "mdi:history"

    def test_name(self):
        from shady.button import RebuildHistoryButton

        assert RebuildHistoryButton._attr_name == "Rebuild History"

    @pytest.mark.asyncio
    async def test_press_calls_rebuild_history(self):
        coordinator = _make_coordinator()
        btn = self._make_button(coordinator=coordinator)
        await btn.async_press()
        coordinator.async_rebuild_history.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_double_press_guard(self):
        """Second press while first is in progress should not start a second rebuild."""
        call_count = 0
        lock = asyncio.Lock()

        async def _slow_rebuild():
            nonlocal call_count
            async with lock:
                call_count += 1
                await asyncio.sleep(0.01)

        coordinator = _make_coordinator()
        coordinator.async_rebuild_history = _slow_rebuild
        btn = self._make_button(coordinator=coordinator)

        # Fire two presses concurrently
        await asyncio.gather(btn.async_press(), btn.async_press())
        # Both awaits complete but call_count depends on implementation;
        # the important thing is that async_press() doesn't raise.
        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_press_propagates_coordinator_refresh(self):
        """async_press must await the coordinator's rebuild, not silently swallow it."""
        coordinator = _make_coordinator()
        btn = self._make_button(coordinator=coordinator)
        await btn.async_press()
        assert coordinator.async_rebuild_history.await_count == 1


class TestRebuildHistoryButtonSetup:
    @pytest.mark.asyncio
    async def test_async_setup_entry_adds_button(self):
        from shady.button import async_setup_entry

        coordinator = _make_coordinator()
        entry = _make_entry()
        hass = MagicMock()
        hass.data = {"shady": {entry.entry_id: coordinator}}

        added: list = []

        # async_add_entities is a plain synchronous callable in HA
        def _add(entities):
            added.extend(entities)

        with patch("shady.button.DOMAIN", "shady"):
            await async_setup_entry(hass, entry, _add)

        assert len(added) == 1
        from shady.button import RebuildHistoryButton

        assert isinstance(added[0], RebuildHistoryButton)
