#! /usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import json
import os
import threading
import asyncio

from zeroconf import IPVersion, ServiceBrowser, ServiceStateChange, Zeroconf

try:
    import pylutron_caseta.leap
    from pylutron_caseta.pairing import async_pair
    from pylutron_caseta.smartbridge import Smartbridge
except ImportError:
    raise ImportError("'Required Python libraries missing.  Run 'pip3 install pylutron_caseta' in Terminal window, then reload plugin.")


class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)

        pfmt = logging.Formatter('%(asctime)s.%(msecs)03d\t[%(levelname)8s] %(name)20s.%(funcName)-25s%(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        self.plugin_file_handler.setFormatter(pfmt)
        self.logLevel = int(pluginPrefs.get("logLevel", logging.INFO))
        self.logger.debug(f"LogLevel = {self.logLevel}")
        self.indigo_log_handler.setLevel(self.logLevel)
        self.plugin_file_handler.setLevel(self.logLevel)

        self.pluginId = pluginId
        self.pluginPrefs = pluginPrefs
        self.triggers = []
        self.event_loop = None
        self.async_thread = None
        self.session = None
        self.found_hubs = {}    # zeroconf discovered devices

        self.known_hubs = {}
        self.known_dimmers = {}
        self.known_switches = {}
        self.known_covers = {}

        self.active_systems = {}
        self.active_sensors = {}
        self.active_cameras = {}
        self.active_locks = {}

        self.system_refresh_task: asyncio.Task | None = None
        self.websocket_reconnect_task: asyncio.Task | None = None
        self.systems: dict[int, SystemType] = {}

        self.updateFrequency = float(self.pluginPrefs.get('updateFrequency', "15")) * 60.0
        self.logger.debug(f"updateFrequency = {self.updateFrequency}")

    def ssl_file_path(self, address: str) -> str:
        path = f"{indigo.server.getInstallFolderPath()}/Preferences/Plugins/{self.pluginId}/{address}"
        if not os.path.exists(path):
            os.makedirs(path)
        return path

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        self.logger.threaddebug(f"closedPrefsConfigUi, valuesDict = {valuesDict}")
        if not userCancelled:
            self.logLevel = int(valuesDict.get("logLevel", logging.INFO))
            self.indigo_log_handler.setLevel(self.logLevel)
            self.plugin_file_handler.setLevel(self.logLevel)
            self.logger.debug(f"LogLevel = {self.logLevel}")

    def startup(self):
        self.logger.debug("startup")
        self.async_thread = threading.Thread(target=self.run_async_thread)
        self.async_thread.start()
        zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        services = ["_lutron._tcp.local."]
        browser = ServiceBrowser(zeroconf, services, handlers=[self.on_service_state_change])
        self.logger.debug("startup complete")

    def on_service_state_change(self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        self.logger.debug(f"Service {name} of type {service_type} state changed: {state_change}")

        if state_change in [ServiceStateChange.Added, ServiceStateChange.Updated]:
            info = zeroconf.get_service_info(service_type, name)
            if service_type == "_lutron._tcp.local.":
                if info.server not in self.found_hubs:
                    self.found_hubs[info.server] = f"{info.server} ({info.properties.get(b'SYSTYPE', b'Unknown').decode('utf-8')})"
                    self.logger.debug(f"Adding Found Hub: {info}")

        elif state_change is ServiceStateChange.Removed:
            info = zeroconf.get_service_info(service_type, name)
            if service_type == "_lutron._tcp.local.":
                if info.server in self.found_hubs:
                    del self.found_hubs[info.server]

    def run_async_thread(self):
        self.logger.debug("run_async_thread starting")
        self.event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.event_loop)
        self.event_loop.run_until_complete(self.async_main())
        self.event_loop.close()
        self.logger.debug("run_async_thread ending")

    ##############################################################################################

    async def async_main(self):
        self.logger.debug("async_main starting")

        while True:
            await asyncio.sleep(1.0)
            if self.stopThread:
                self.logger.debug("async_main: stopping")
                break

    async def lap_pair(self, address: str):
        """
        Perform LAP pairing.

        This program connects to a Lutron bridge device and initiates the LAP pairing
        process. The user will be prompted to press a physical button on the bridge, and
        certificate files will be generated on the local computer.
        """
        self.logger.debug(f"lap_pair start: address = {address}")

        try:
            data = await async_pair(address)
        except Exception as e:
            self.logger.warning(f"Exception while pairing: {e}")
            return

        self.logger.threaddebug(f"lap_pair: data = {data}")

        with open(f"{self.ssl_file_path(address)}/leapHub-bridge.crt", "w") as f:
            f.write(data["ca"])
        with open(f"{self.ssl_file_path(address)}/leapHub.crt", "w") as f:
            f.write(data["cert"])
        with open(f"{self.ssl_file_path(address)}/leapHub.key", "w") as f:
            f.write(data["key"])

        self.logger.debug(f"lap_pair complete: hub version = {data['version']}")

    ##################
    # Device Methods
    ##################

    def getDeviceConfigUiValues(self, pluginProps, typeId, devId):
        self.logger.threaddebug(f"getDeviceConfigUiValues, typeId = {typeId}, devId = {devId}, pluginProps = {pluginProps}")
        valuesDict = indigo.Dict(pluginProps)
        errorsDict = indigo.Dict()
        return valuesDict, errorsDict

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        self.logger.threaddebug(f"validateDeviceConfigUi, typeId = {typeId}, devId = {devId}, valuesDict = {valuesDict}")

        if typeId == 'leapHub':
            self.event_loop.create_task(self.lap_pair(valuesDict['address']))

        return True, valuesDict

    def deviceStartComm(self, device):
        self.logger.debug(f"{device.name}: Starting Device")

        if device.deviceTypeId == 'leapHub' and device.pluginProps['status'] == 'paired':
            path = self.ssl_file_path(device.address)
            if not os.path.exists(path):
                self.logger.warning(f"{device.name}: SSL files not found in {path}")
                return

            bridge = Smartbridge.create_tls(device.address, f"{path}/leapHub.key", f"{path}/leapHub.crt", f"{path}/leapHub-bridge.crt")
            self.event_loop.create_task(bridge.connect())

        device.stateListOrDisplayStateIdChanged()

    def deviceStopComm(self, device):
        self.logger.debug(f"{device.name}: Stopping Device")

    ########################################
    # Trigger (Event) handling
    ########################################

    def triggerStartProcessing(self, trigger):
        self.logger.debug(f"{trigger.name}: Adding Trigger")
        assert trigger.id not in self.triggers
        self.triggers.append(trigger.id)

    def triggerStopProcessing(self, trigger):
        self.logger.debug(f"{trigger.name}: Removing Trigger")
        assert trigger.id in self.triggers
        self.triggers.remove(trigger.id)

    def triggerCheck(self, event):
        for trigger_id in self.triggers:
            trigger = indigo.triggers[trigger_id]
            device = indigo.devices[int(trigger.pluginProps['system'])]
            self.logger.debug(f"{trigger.name}: triggerCheck system = {device.address}, event_type = {trigger.pluginProps['event_type']}")
            if str(device.address) == str(event.system_id) and str(trigger.pluginProps['event_type']) == str(event.event_type):
                indigo.trigger.execute(trigger)

    ########################################
    # callbacks from device creation UI
    ########################################

    def get_hub_list(self, filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.threaddebug(f"get_system_list: typeId = {typeId}, targetId = {targetId}, filter = {filter}, valuesDict = {valuesDict}")
        hubs = [(k, v) for k, v in self.found_hubs.items()]
        self.logger.debug(f"get_hub_list: hubs = {hubs}")
        return hubs

    def get_device_list(self, filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.threaddebug(f"get_device_list: typeId = {typeId}, targetId = {targetId}, filter = {filter}, valuesDict = {valuesDict}")
        try:
            devices = []
            system_id = int(valuesDict["system"])
            if filter == "camera":
                devices = [
                    (device_id, device.name)
                    for device_id, device in self.known_cameras[system_id].items()
                ]
            elif filter == "lock":
                devices = [
                    (device_id, device.name)
                    for device_id, device in self.known_locks[system_id].items()
                ]
            elif filter == "sensor":
                devices = [
                    (device_id, f"{device.name} - {str(device.type)}")
                    for device_id, device in self.known_sensors[system_id].items()
                ]
            else:
                self.logger.debug(f"get_device_list: unknown filter = {filter}")

        except KeyError:
            devices = []
        self.logger.debug(f"get_device_list: devices = {devices}")
        return devices

    # doesn't do anything, just needed to force other menus to dynamically refresh
    def menuChanged(self, valuesDict=None, typeId=None, devId=None):  # noqa
        self.logger.threaddebug(f"menuChanged: typeId = {typeId}, devId = {devId}, valuesDict = {valuesDict}")
        return valuesDict

    ########################################
    # Menu and Action methods
    ########################################

    def actionControlDevice(self, action, device):
        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            return

        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            return

