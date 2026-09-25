"""Regression checks for Parley's interaction/panel self-healing layers."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from bot.core import ParleyBot, persistent_items
from bot.services.panels import PanelService
from bot.tasks import BackgroundTasks
from bot.views import base, welcome


def test_ack_watchdog_is_armed_before_the_first_defer():
    source = inspect.getsource(base.acknowledge)
    assert source.index("arm_interaction_ack_watchdog(interaction)") < source.index("interaction.response.defer")
    assert base.ACK_WATCHDOG_SECONDS < 1.0


def test_top_level_buttons_have_two_dispatch_routes():
    callback = inspect.getsource(welcome.ActionButton.callback)
    event = inspect.getsource(ParleyBot.on_interaction)
    assert "await acknowledge(interaction)" in callback
    assert "claim_action_interaction" in callback
    assert "dispatch_action" in callback
    assert "TOP_LEVEL_ACTION_ID" in event
    assert "claim_action_interaction" in event
    assert "dispatch_action" in event


def test_top_level_buttons_use_static_persistent_router_not_dynamic_items():
    from bot.views.welcome import ActionButton, action_router_view, registered_actions

    assert issubclass(ActionButton, __import__("discord").ui.Button)
    view = action_router_view()
    ids = {child.custom_id for child in view.children}
    assert ids == {f"wp:act:{action}" for action in registered_actions()}
    assert view.timeout is None


def test_dynamic_registry_is_repaired_when_mixed_view_stops():
    source = inspect.getsource(ParleyBot._install_dynamic_registry_guard)
    ensure = inspect.getsource(ParleyBot._ensure_dynamic_item_registry)
    event = inspect.getsource(ParleyBot.on_interaction)
    assert "store.remove_view = guarded_remove" in source
    assert "store.add_dynamic_items(*missing)" in ensure
    assert "dynamic_current_click_recovered" in event


def test_unknown_parley_components_still_get_timeout_protection():
    source = inspect.getsource(ParleyBot.on_interaction)
    assert 'custom_id.startswith("wp:")' in source
    assert "arm_interaction_ack_watchdog(interaction)" in source


def test_process_start_reposts_public_entry_panels():
    source = inspect.getsource(ParleyBot.on_ready)
    assert "refresh_entry_panels(repost=True)" in source
    assert "cleanup_orphan_entry_panels" in source


def test_reconnects_verify_controls_again():
    resumed = inspect.getsource(ParleyBot.on_resumed)
    ready = inspect.getsource(ParleyBot.on_ready)
    repair = inspect.getsource(ParleyBot._repair_after_resume)
    assert "_repair_after_resume" in resumed
    assert "_repair_after_resume" in ready
    assert "refresh_entry_panels(repost=True)" in repair
    assert "cleanup_orphan_entry_panels" in repair


def test_panel_refreshes_are_paced_to_reduce_discord_429s():
    source = inspect.getsource(PanelService.refresh_entry_panels)
    listings = inspect.getsource(PanelService.refresh_active_listing_views)
    assert "STARTUP_PANEL_SPACING_SECONDS" in source
    assert "LISTING_REFRESH_SPACING_SECONDS" in listings


def test_long_running_bot_has_event_loop_and_periodic_control_monitoring():
    start = inspect.getsource(BackgroundTasks.start)
    watchdog = inspect.getsource(BackgroundTasks._event_loop_watchdog)
    maintenance = inspect.getsource(BackgroundTasks.maintenance_tick)
    assert "_event_loop_watchdog" in start
    assert "event_loop.lag" in watchdog
    assert "discord.gateway_latency" in watchdog
    assert "refresh_entry_panels(repost=should_repost)" in maintenance
    assert "refresh_active_listing_views" in maintenance
    assert "ENTRY_PANEL_REPOST_SECONDS" in maintenance
    assert __import__("bot.tasks", fromlist=["ENTRY_PANEL_REPOST_SECONDS"]).ENTRY_PANEL_REPOST_SECONDS <= 30 * 60


async def test_static_action_router_survives_transient_view_removal():
    """Regression for the exact long-running button failure seen in production.

    Top-level actions used to be DynamicItems. discord.py keeps DynamicItem
    templates in one global registry and ViewStore.remove_view removes a
    template when any view containing it stops. Because Parley has temporary
    10-minute menus, that made all old public action buttons die after a while.

    Top-level actions are now ordinary persistent buttons. Removing a transient
    message-specific view must not touch the global router.
    """
    import discord
    from bot.views.welcome import ActionButton, action_router_view

    persistent_items()  # imports every module that registers top-level actions
    store = discord.ui.view.ViewStore(SimpleNamespace())
    router = action_router_view()
    store.add_view(router)
    key = (discord.ComponentType.button.value, "wp:act:find")
    assert key in store._views[None]

    transient = discord.ui.View(timeout=600)
    transient.add_item(ActionButton("find", label="Find Partners"))
    transient.add_item(discord.ui.Button(label="Cancel", custom_id="transient:cancel"))
    store.add_view(transient, message_id=123456789)
    store.remove_view(transient)

    assert key in store._views[None]
