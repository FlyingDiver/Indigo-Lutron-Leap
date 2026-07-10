# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An Indigo Domotics plugin that lets an Indigo home automation server control Lutron systems (Caséta, RadioRA 2/3, Homeworks QSX) over the LEAP protocol. Runs as a long-lived process inside the Indigo Server, not as a standalone app — there is no local build, lint, or test suite. Development consists of editing the plugin source, then installing/reloading the plugin in an actual Indigo Server to exercise it.

All plugin code lives in `Lutron Leap.indigoPlugin/Contents/Server Plugin/`:
- `plugin.py` — the entire plugin implementation (single file, `Plugin(indigo.PluginBase)`)
- `Devices.xml`, `Actions.xml`, `Events.xml`, `MenuItems.xml`, `PluginConfig.xml` — Indigo's declarative UI/config definitions for device types, plugin actions, triggers, menu commands, and preferences
- `requirements.txt` — `zeroconf`, `pylutron_caseta` (third-party deps; `pylutron_caseta` provides the LEAP protocol client)
- `Contents/Packages/` — vendored/installed third-party dependencies (generated, not hand-edited)

`indigo` is a module injected by the Indigo Server host process at runtime — it does not exist in a normal Python environment, so this code cannot be run or unit-tested outside of Indigo.

## Architecture

**Threading model:** Indigo calls plugin methods (`deviceStartComm`, `actionControlDimmerRelay`, etc.) synchronously from Indigo's own thread(s). `pylutron_caseta`'s `Smartbridge` is asyncio-based, so `startup()` spins up a dedicated background thread (`run_async_thread`) running its own `asyncio` event loop (`self.event_loop`). Synchronous Indigo callbacks hand work off to the async side via `self.event_loop.create_task(...)`; async code hands results back to Indigo by calling `device.updateStateOnServer(...)` etc. directly (these calls are safe from the async thread).

**Bridge/device relationship:** A "Lutron Bridge" Indigo device (`leapBridge` type) represents one physical Lutron hub and owns one `Smartbridge` connection. All other Indigo device types (`leapSwitch`, `leapDimmer`, `leapShade`, `leapFan`, `leapColor`, `occupancy_group`) reference a bridge via `pluginProps["bridge"]` (the Indigo bridge device ID) and a LEAP-side ID via `pluginProps["device"]`. Devices only start talking to the bridge once `bridge_connected_events[bridge_id]` is set (see `async_start_device`), so device startup always waits for its bridge to finish connecting first.

**Pairing:** Bridges must be paired once via `lap_pair()`, which calls `pylutron_caseta.pairing.async_pair` and writes TLS client cert/key/CA files under `<Indigo install>/Preferences/Plugins/<pluginId>/<bridge address>/leapBridge{.crt,.key,-CA.crt}` (`ssl_file_path`). Subsequent connections (`bridge_connect`) use `Smartbridge.create_tls(...)` with those files.

**Discovery:** Bridges are discovered on the LAN via zeroconf/Bonjour (`_lutron._tcp.local.` service type); found bridges populate `self.found_bridges` for the device-creation UI (see `get_found_bridges`, `menuChangedConfig`).

**In-memory state dictionaries** (all keyed by Indigo bridge device ID, several nested by LEAP device/button/scene/group ID) mirror what the bridge knows and are rebuilt on every `bridge_connect`:
- `leap_bridges` — Indigo bridge ID → `Smartbridge` instance
- `leap_known_devices`, `leap_known_groups`, `leap_buttons`, `leap_scenes`, `leap_areas` — raw LEAP-side catalogs used for menu population and path-name generation
- `leap_devices` — `"bridge_id:leap_device_id"` address string → Indigo device ID, used to route bridge events back to the right Indigo device

**Device creation:** There is no manual device creation — "Create Devices from Bridge" (`menu_create_devices_for_bridge` → `create_devices_for_bridge`) walks the bridge's known devices/occupancy groups and calls `create_leap_device` for each, mapping LEAP device types to Indigo device types via `pylutron_caseta`'s `_LEAP_DEVICE_TYPES`/`RA3_OCCUPANCY_SENSOR_DEVICE_TYPES` tables. Device names are built as an area/control-station/device path (`get_area_path` walks the area tree recursively via `parent_id`). Buttons, scenes, and Pico remotes have no Indigo device representation — they exist only as entries in `leap_buttons`/`leap_scenes` and can only be used as trigger sources or action targets, never as devices.

**Events → Indigo state:** LEAP push events arrive via callbacks registered per-device/button/occupancy-group (`bridge.add_subscriber`, `bridge.add_button_subscriber`, `bridge.add_occupancy_subscriber`) and are dispatched to `device_event`, `button_event`, `occupancy_event`, which update Indigo device states and fire matching Indigo triggers (`indigo.triggers.iter("self")`, matched by trigger type and address/props).

**Button multi-press / linked devices:** Button presses are queued (`keypress_queue`) and drained on a 100ms poll loop (`buttonMultiPressCheck`, `async_main`) to detect multi-click patterns within `click_timeout` seconds, firing `multiButtonPress` triggers with a matching click count. Separately, `linked_device_list` (persisted in `pluginPrefs["linked_devices"]` as JSON) lets a button press directly toggle a non-Lutron Indigo device — managed via the "Manage Linked Devices" UI (`add_linked_device`/`delete_linked_devices`/`list_linked_devices`).
