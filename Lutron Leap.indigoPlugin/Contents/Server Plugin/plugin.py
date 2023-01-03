#! /usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import json
import os
import time
from datetime import timedelta
import threading
import asyncio
from zeroconf import IPVersion, ServiceBrowser, ServiceStateChange, Zeroconf
from device_types import LEAP_DEVICE_TYPES

try:
    from pylutron_caseta.pairing import async_pair
    from pylutron_caseta.smartbridge import Smartbridge
except ImportError:
    raise ImportError("'Required Python libraries missing.  Run 'pip3 install pylutron_caseta' in Terminal window, then reload plugin.")

# Indigo Custom Device Types
DEV_DIMMER = "leapDimmer"
DEV_SWITCH = "leapSwitch"
DEV_SHADE  = "leapShade"
DEV_FAN    = "leapFan"

_FAN_SPEED_MAP = {
    0: "Off",
    1: "Low",
    2: "Medium",
    3: "High",
}

def clamp(n, minn, maxn):
    return max(min(maxn, n), minn)

class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)

        pfmt = logging.Formatter('%(asctime)s.%(msecs)03d\t[%(levelname)8s] %(name)20s.%(funcName)-25s%(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        self.plugin_file_handler.setFormatter(pfmt)
        self.logLevel = int(pluginPrefs.get("logLevel", logging.DEBUG))
        self.logger.debug(f"LogLevel = {self.logLevel}")
        self.indigo_log_handler.setLevel(self.logLevel)
        self.plugin_file_handler.setLevel(self.logLevel)

        self.pluginId = pluginId
        self.pluginPrefs = pluginPrefs
        self.triggers = []
        self.event_loop = None
        self.async_thread = None

        self.found_bridges = {}  # zeroconf discovered bridges
        self.bridge_data = {}  # all devices from all bridges

        self.leap_bridges = {}  # devices with matching Indigo devices
        self.leap_devices = {}
        self.linkedDeviceList = {}

        self.lastKeyTime = time.time()
        self.lastKeyAddress = ""
        self.lastKeyTaps = 0
        self.newKeyPress = False
        self.click_timeout = float(self.pluginPrefs.get("click_timeout", "0.5"))

    def ssl_file_path(self, address: str) -> str:
        folder = f"{indigo.server.getInstallFolderPath()}/Preferences/Plugins/{self.pluginId}/{address}"
        if not os.path.exists(folder):
            os.makedirs(folder)
        return folder + "/leapBridge"

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        self.logger.threaddebug(f"closedPrefsConfigUi, valuesDict = {valuesDict}")
        if not userCancelled:
            self.logLevel = int(valuesDict.get("logLevel", logging.INFO))
            self.indigo_log_handler.setLevel(self.logLevel)
            self.plugin_file_handler.setLevel(self.logLevel)
            self.logger.debug(f"LogLevel = {self.logLevel}")

    def startup(self):
        self.logger.debug("startup")
        savedList = self.pluginPrefs.get("linkedDevices", None)
        if savedList:
            self.linkedDeviceList = json.loads(savedList)
            self.logLinkedDevices()

        indigo.devices.subscribeToChanges()

        threading.Thread(target=self.run_async_thread).start()
        ServiceBrowser(Zeroconf(ip_version=IPVersion.V4Only), ["_lutron._tcp.local."], handlers=[self.on_service_state_change])
        self.logger.debug("startup complete")

    def on_service_state_change(self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        self.logger.threaddebug(f"Service {name} of type {service_type} state changed: {state_change}")

        if state_change in [ServiceStateChange.Added, ServiceStateChange.Updated]:
            info = zeroconf.get_service_info(service_type, name)
            if info.server not in self.found_bridges:
                self.found_bridges[info.server] = f"{info.properties.get(b'SYSTYPE', b'Unknown').decode('utf-8')} @ {info.server}"
                self.logger.threaddebug(f"Adding Found Bridge: {info}")
        elif state_change is ServiceStateChange.Removed:
            info = zeroconf.get_service_info(service_type, name)
            if info.server in self.found_bridges:
                del self.found_bridges[info.server]

        ################################################################################
        #
        # delegate methods for indigo.devices.subscribeToChanges()
        #
        ################################################################################

    def deviceDeleted(self, delDevice):
        indigo.PluginBase.deviceDeleted(self, delDevice)

        for linkID in list(self.linkedDeviceList.keys()):
            linkItem = self.linkedDeviceList[linkID]
            if (delDevice.id == int(linkItem["buttonDevice"])) or (delDevice.id == int(linkItem["buttonLEDDevice"])) or (
                    delDevice.id == int(linkItem["controlledDevice"])):
                self.logger.info(f"A linked device ({delDevice.name}) has been deleted.  Deleting link: {linkItem['name']}")
                del self.linkedDeviceList[linkID]
                self.logLinkedDevices()

                indigo.activePlugin.pluginPrefs["linkedDevices"] = json.dumps(self.linkedDeviceList)

    def deviceUpdated(self, oldDevice, newDevice):
        indigo.PluginBase.deviceUpdated(self, oldDevice, newDevice)

        for linkName, linkItem in self.linkedDeviceList.items():
            controlledDevice = indigo.devices[int(linkItem["controlledDevice"])]
            buttonDevice = indigo.devices[int(linkItem["buttonDevice"])]

            if oldDevice.id == controlledDevice.id:

                self.logger.debug(f"A linked device ({controlledDevice.name}) has been updated: {controlledDevice.onState}")
                try:
                    buttonLEDDevice = indigo.devices[int(linkItem["buttonLEDDevice"])]
                except Exception as e:
                    pass
                else:
                    if controlledDevice.onState:
                        indigo.device.turnOn(buttonLEDDevice.id)
                    else:
                        indigo.device.turnOff(buttonLEDDevice.id)

    ##############################################################################################

    def run_async_thread(self):
        self.logger.debug("run_async_thread starting")
        self.event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.event_loop)
        self.event_loop.run_until_complete(self.async_main())
        self.event_loop.close()
        self.logger.debug("run_async_thread ending")

    async def async_main(self):
        self.logger.debug("async_main starting")

        while True:
            await asyncio.sleep(0.1)
            await self.buttonMultiPressCheck()
            if self.stopThread:
                self.logger.debug("async_main: stopping")
                break

    async def lap_pair(self, deviceID, address: str):
        """
        Perform LAP pairing.

        This program connects to a Lutron bridge device and initiates the LAP pairing
        process. The user will be prompted to press a physical button on the bridge, and
        certificate files will be generated on the local computer.
        """
        self.logger.info(f"Starting pairing process for Bridge {address}, please press the pairing button on the Bridge.")

        try:
            data = await async_pair(address)
        except Exception as e:
            self.logger.warning(f"Exception while pairing: {e}")
            self.logger.warning(f"Possibly not a Leap capable Bridge: {address}")
            return

        self.logger.debug(f"lap_pair: data = {data}")

        with open(f"{self.ssl_file_path(address)}-CA.crt", "w") as f:
            f.write(data["ca"])
        with open(f"{self.ssl_file_path(address)}.crt", "w") as f:
            f.write(data["cert"])
        with open(f"{self.ssl_file_path(address)}.key", "w") as f:
            f.write(data["key"])

        self.logger.info(f"Pairing complete: Bridge version is {data['version']}.")
        dev = indigo.devices[deviceID]
        newProps = dev.pluginProps
        newProps['paired'] = "true"
        dev.replacePluginPropsOnServer(newProps)
        update_list = [
            {'key': "protocol_version", 'value': data['version']},
            {'key': "status", 'value': "Paired"},
        ]
        dev.updateStatesOnServer(update_list)

    async def bridge_connect(self, indigo_bridge_dev):
        path = self.ssl_file_path(indigo_bridge_dev.address)
        if not os.path.exists(path + ".key"):
            self.logger.warning(f"{indigo_bridge_dev.name}: SSL files not found at {path}.  Unable to connect.")
            return

        self.logger.debug(f"{indigo_bridge_dev.name}: Creating bridge at {indigo_bridge_dev.address}")
        bridge = Smartbridge.create_tls(indigo_bridge_dev.address, f"{path}.key", f"{path}.crt", f"{path}-CA.crt")

        self.logger.debug(f"{indigo_bridge_dev.name}: Connecting Bridge")
        await bridge.connect()
        self.logger.debug(f"{indigo_bridge_dev.name}: Bridge Connected")
        self.leap_bridges[indigo_bridge_dev.id] = bridge
        indigo_bridge_dev.updateStateOnServer('status', "Connected")

        self.bridge_data[indigo_bridge_dev.id] = {'devices': {}, 'buttons': {}, 'scenes': {}, 'areas': {}, 'occupancy_groups': {}}

        for dev in bridge.get_devices().values():
            self.logger.debug(f"Found device: {dev['name']}")

            bridge.add_subscriber(dev['device_id'], lambda leap_device_id=dev['device_id']: self.device_event(indigo_bridge_dev.id, leap_device_id))
            self.bridge_data[indigo_bridge_dev.id]['devices'][dev['device_id']] = dev

        for button in bridge.get_buttons().values():
            self.logger.debug(f"{indigo_bridge_dev.name}: Found Button: {button['name']} ({button['parent_device']}) - {button['device_id']}")
            self.bridge_data[indigo_bridge_dev.id]['buttons'][button['device_id']] = button
            bridge.add_button_subscriber(button['device_id'],
                lambda event_type, button_device=button['parent_device'], button_id=button['device_id']: self.button_event(
                                             indigo_bridge_dev.id, button_device, button_id, event_type))

        for scene in bridge.get_scenes().values():
            self.bridge_data[indigo_bridge_dev.id]['scenes'][scene['scene_id']] = scene['name']

        for area in bridge.areas.values():
            self.logger.debug(f"{indigo_bridge_dev.name}: Found Area: {area}")
            self.bridge_data[indigo_bridge_dev.id]['areas'][area['id']] = area

        for occupancy_group in bridge.occupancy_groups.values():
            self.logger.debug(f"{indigo_bridge_dev.name}: Found Occupancy Group: {occupancy_group}")
            self.bridge_data[indigo_bridge_dev.id]['occupancy_groups'][occupancy_group['occupancy_group_id']] = occupancy_group
            bridge.add_occupancy_subscriber(occupancy_group['occupancy_group_id'],
                lambda group_id=occupancy_group['occupancy_group_id']: self.occupancy_event(indigo_bridge_dev.id, group_id))

    def update_dev_states(self, dev, data):
        update_list = [
            {'key': "area", 'value': data['area']},
            {'key': "button_groups", 'value': data['button_groups']},
            {'key': "current_state", 'value': data['current_state']},
            {'key': "device_id", 'value': data['device_id']},
            {'key': "device_name", 'value': data['device_name']},
            {'key': "fan_speed", 'value': data['fan_speed']},
            {'key': "model", 'value': data['model']},
            {'key': "name", 'value': data['name']},
            {'key': "occupancy_sensors", 'value': data['occupancy_sensors']},
            {'key': "serial", 'value': data['serial']},
            {'key': "tilt", 'value': data['tilt']},
            {'key': "type", 'value': data['type']},
            {'key': "zone", 'value': data['zone']},
        ]
        try:
            dev.updateStatesOnServer(update_list)
        except Exception as e:
            self.logger.error(f"{dev.name}: failed to update states: {e}")

    ##############################################################################################
    # Event Handlers
    ##############################################################################################

    def device_event(self, bridge_id, device_id):
        self.logger.debug(f"device_event: bridge_id = {bridge_id}, device_id = {device_id}")

        bridge = self.leap_bridges[bridge_id]
        data = bridge.get_device_by_id(device_id)
        self.logger.debug(f"device_event: data = {data}")
        self.bridge_data[bridge_id]['devices'][data['device_id']] = data

        dev = indigo.devices[self.leap_devices[f"{bridge_id}:{device_id}"]]
        self.update_dev_states(dev, data)

        if dev.deviceTypeId == DEV_DIMMER:
            level = float(data['current_state'])
            if int(level) == 0:
                dev.updateStateOnServer("onOffState", False)
            else:
                dev.updateStateOnServer("brightnessLevel", int(level))
            self.logger.debug(f"device_event: Dimmer {dev.name} level set to {level}%")

        elif dev.deviceTypeId == DEV_SWITCH:
            dev.updateStateOnServer("onOffState", data['current_state'])
            self.logger.debug(f"device_event: Switch {dev.name} state set to {data['current_state']}")

        elif dev.deviceTypeId == DEV_FAN:
            fan_speed = data['fan_speed']
            self.logger.debug(f"{dev.name}: Fan speed is now {fan_speed}")
            if fan_speed == "Off":
                dev.updateStateOnServer("speedIndex", 0, uiValue="Off")
            elif fan_speed == "Low":
                dev.updateStateOnServer("speedIndex", 1, uiValue="Low")
            elif fan_speed == "Medium":
                dev.updateStateOnServer("speedIndex", 2, uiValue="Medium")
            elif fan_speed == "MediumHigh":
                dev.updateStateOnServer("speedIndex", 2, uiValue="MediumHigh")      # MediumHigh is treated as Medium
            elif fan_speed == "High":
                dev.updateStateOnServer("speedIndex", 3, uiValue="High")

        elif dev.deviceTypeId == DEV_SHADE:
            level = float(data['current_state'])
            if int(level) == 0:
                dev.updateStateOnServer("onOffState", False)
            else:
                dev.updateStateOnServer("brightnessLevel", int(level))
            self.logger.debug(f"device_event: Shade {dev.name} set to {level}%")

        else:
            self.logger.debug(f"device_event: Unknown device type: {dev.deviceTypeId}")

    def occupancy_event(self, bridge_id, group_id):
        self.logger.debug(f"occupancy_event: bridge_id = {bridge_id}, group_id = {group_id}")
        data = self.leap_bridges[bridge_id].occupancy_groups[group_id]
        self.logger.debug(f"occupancy_event: data = {data}")

        for triggerID in self.triggers:
            trigger = indigo.triggers[triggerID]
            if trigger.pluginTypeId == "occupancy_event":
                if trigger.pluginProps['occupancy_group'] == f"{bridge_id}:{data['occupancy_group_id']}" and \
                        trigger.pluginProps['event_type'] == data['status']:
                    indigo.trigger.execute(trigger)

    def button_event(self, bridge, button_device, button_id, event_type):
        self.logger.debug(f"button_event: button_device = {button_device}, button_id = {button_id}, event_type = {event_type}")

        button_address = f"{bridge}:{button_id}"
        self.logger.debug(f"button_event: button_address = {button_address}")

        for triggerID in self.triggers:
            trigger = indigo.triggers[triggerID]
            if trigger.pluginTypeId == "buttonEvent":
                self.logger.debug(
                    f"button_event: trigger event_type = {trigger.pluginProps['event_type']}, trigger address = {trigger.pluginProps['button_address']}")
                if trigger.pluginProps['event_type'] == event_type and \
                        trigger.pluginProps['button_address'] == button_address:
                    indigo.trigger.execute(trigger)

        # check for linked devices
        for linkItem in self.linkedDeviceList.values():
            controlledDevice = indigo.devices[int(linkItem["controlledDevice"])]
            buttonAddress = linkItem["buttonAddress"]
            if buttonAddress == button_id:
                self.logger.debug(f"Linked Device Match, buttonAddress: {buttonAddress}, controlledDevice: {controlledDevice.id}")
                indigo.device.toggle(controlledDevice.id)

        # and update the tap count for multi-press triggers, only on PRESS events
        if event_type == "Press":
            if (button_address == self.lastKeyAddress) and (time.time() < (self.lastKeyTime + self.click_timeout)):
                self.lastKeyTaps += 1
            else:
                self.lastKeyTaps = 1

        self.lastKeyAddress = button_address
        self.lastKeyTime = time.time()
        self.newKeyPress = True

    async def buttonMultiPressCheck(self):

        if self.newKeyPress:

            # if last key press hasn't timed out yet, don't do anything
            if time.time() < (self.lastKeyTime + self.click_timeout):
                return

            self.logger.debug(f"buttonMultiPressCheck: Timeout reached for button = {self.lastKeyAddress}, presses = {self.lastKeyTaps}")
            self.newKeyPress = False

            for triggerID in self.triggers:
                trigger = indigo.triggers[triggerID]
                if trigger.pluginTypeId != "multiButtonPress":
                    continue

                if trigger.pluginProps["button_address"] != self.lastKeyAddress:
                    self.logger.threaddebug(f"buttonMultiPressCheck: Skipping Trigger '{trigger.name}', wrong keypad button: {self.lastKeyAddress}")
                    continue

                if self.lastKeyTaps != int(trigger.pluginProps.get("clicks", "1")):
                    self.logger.threaddebug(f"buttonMultiPressCheck: Skipping Trigger {trigger.name}, wrong click count: {self.lastKeyTaps}")
                    continue

                self.logger.debug(f"buttonMultiPressCheck: Executing Trigger '{trigger.name}', keypad button: {self.lastKeyAddress}")
                indigo.trigger.execute(trigger)

    ##################
    # Device Methods
    ##################

    def getDeviceConfigUiValues(self, pluginProps, typeId, devId):
        self.logger.threaddebug(f"getDeviceConfigUiValues, typeId = {typeId}, devId = {devId}, pluginProps = {pluginProps}")
        valuesDict = indigo.Dict(pluginProps)
        errorsDict = indigo.Dict()
        return valuesDict, errorsDict

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        self.logger.debug(f"validateDeviceConfigUi, typeId = {typeId}, devId = {devId}, valuesDict = {valuesDict}")

        if typeId == 'leapBridge':
            self.event_loop.create_task(self.lap_pair(devId, valuesDict['address']))

        return True, valuesDict

    def deviceStartComm(self, device):
        self.logger.debug(f"{device.name}: Starting Device")

        if device.deviceTypeId == 'leapBridge':
            self.leap_bridges[device.id] = None  # create a placeholder for the bridge object, created asynchronously
            if device.pluginProps['paired'] == 'true':
                self.event_loop.create_task(self.bridge_connect(device))
        else:
            self.leap_devices[device.address] = device.id

        device.stateListOrDisplayStateIdChanged()

    def deviceStopComm(self, device):
        self.logger.debug(f"{device.name}: Stopping Device")
        if device.deviceTypeId == 'leapBridge':
            del self.leap_bridges[device.id]

    def start_devices(self):
        self.logger.debug("start_devices")
        self.logger.debug(f"self.leap_bridges = {self.leap_bridges}")
        for device_id in self.leap_devices.values():
            self.logger.debug(f"start_devices: Working on device_id = {device_id}")
            device = indigo.devices[device_id]
            self.logger.debug(f"start_devices: props = {device.pluginProps}")
            bridge = self.leap_bridges[device.pluginProps['bridge']]
            self.logger.debug(f"start_devices: bridge = {bridge}")
            if bridge is not None:
                self.logger.debug(f"start_devices: Starting device {device.name}")
                data = bridge.get_device_by_id(device.pluginProps['device'])
                self.logger.debug(f"start_devices: data = {data}")

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

    ########################################
    # callbacks from device creation UI
    ########################################

    def get_found_bridges(self, filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.threaddebug(f"get_found_bridges: typeId = {typeId}, targetId = {targetId}, filter = {filter}, valuesDict = {valuesDict}")
        bridges = [(k, v) for k, v in self.found_bridges.items()]
        self.logger.threaddebug(f"get_found_bridges: bridges = {bridges}")
        return bridges

    def get_bridge_list(self, filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.threaddebug(f"get_bridge_list: typeId = {typeId}, targetId = {targetId}, filter = {filter}, valuesDict = {valuesDict}")
        bridges = [(k, indigo.devices[int(k)].name) for k in self.leap_bridges.keys()]
        self.logger.threaddebug(f"get_bridge_list: bridges = {bridges}")
        return bridges

    def get_scene_list(self, filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.threaddebug(f"get_scene_list: typeId = {typeId}, targetId = {targetId}, filter = {filter}, valuesDict = {valuesDict}")
        scenes = [(k, self.bridge_data[targetId]['scenes'][k]) for k in self.bridge_data[targetId]['scenes']]
        self.logger.threaddebug(f"get_scene_list: scenes = {scenes}")
        return scenes

    def get_button_list(self, filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.threaddebug(f"get_button_list: typeId = {typeId}, targetId = {targetId}, filter = {filter}, valuesDict = {valuesDict}")
        bridge_id = valuesDict.get('bridge', targetId)
        buttons = []
        if bridge_id:
            self.logger.threaddebug(f"get_button_list: bridge_info = {bridge_id}, {self.bridge_data}")
            for button in self.bridge_data[int(bridge_id)]['buttons'].values():
                address = f"{bridge_id}:{button['device_id']}"
                name = f"{button['name']} - {button['device_id']}"
                buttons.append((address, name))
        self.logger.threaddebug(f"get_button_list: buttons = {buttons}")
        return buttons

    def get_occupancy_group_list(self, filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.threaddebug(f"get_occupancy_group_list: typeId = {typeId}, targetId = {targetId}, filter = {filter}, valuesDict = {valuesDict}")
        bridge_id = valuesDict.get('bridge', targetId)
        occupancy_groups = []
        if bridge_id:
            self.logger.threaddebug(f"get_occupancy_group_list: bridge_info = {bridge_id}, {self.bridge_data[int(bridge_id)]['occupancy_groups']}")
            for group in self.bridge_data[int(bridge_id)]['occupancy_groups'].values():
                address = f"{bridge_id}:{group['occupancy_group_id']}"
                name = f"{group['name']} - {group['occupancy_group_id']}"
                occupancy_groups.append((address, name))
        self.logger.threaddebug(f"get_occupancy_group_list: occupancy_groups = {occupancy_groups}")
        return occupancy_groups

    def linkable_devices(self, filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.threaddebug(f"linkable_devices, typeId = {typeId}, targetId = {targetId}, valuesDict = {valuesDict}")
        retList = []
        for dev in indigo.devices:
            if hasattr(dev, "onState"):
                if dev.pluginId != self.pluginId:
                    retList.append((dev.id, dev.name))
        retList.sort(key=lambda tup: tup[1])
        return retList

    # doesn't do anything, just needed to force other menus to dynamically refresh
    def menuChanged(self, valuesDict=None, typeId=None, devId=None):  # noqa
        self.logger.threaddebug(f"menuChanged: typeId = {typeId}, devId = {devId}, valuesDict = {valuesDict}")
        return valuesDict

    ########################################
    # Relay / Dimmer / Shade
    ########################################
    def actionControlDimmerRelay(self, action, dev):
        self.logger.debug(f"{dev.name}: actionControlDimmerRelay: action = {action}")

        bridge = self.leap_bridges[dev.pluginProps["bridge"]]

        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            self.event_loop.create_task(bridge.turn_on(dev.pluginProps["device"]))

        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            self.event_loop.create_task(bridge.turn_off(dev.pluginProps["device"]))

        elif action.deviceAction == indigo.kDeviceAction.Toggle:
            if dev.onState:
                self.event_loop.create_task(bridge.turn_off(dev.pluginProps["device"]))
            else:
                self.event_loop.create_task(bridge.turn_on(dev.pluginProps["device"]))

        elif action.deviceAction == indigo.kDeviceAction.SetBrightness:
            self.event_loop.create_task(bridge.set_value(dev.pluginProps["device"], clamp(action.actionValue, 0, 100)))

        elif action.deviceAction == indigo.kDimmerRelayAction.BrightenBy:
            self.event_loop.create_task(bridge.set_value(dev.pluginProps["device"], clamp(dev.brightness + action.actionValue, 0, 100)))

        elif action.deviceAction == indigo.kDimmerRelayAction.DimBy:
            self.event_loop.create_task(bridge.set_value(dev.pluginProps["device"], clamp(dev.brightness - action.actionValue, 0, 100)))

    ########################################
    # Fans
    ########################################

    def actionControlSpeedControl(self, action, dev):
        self.logger.debug(f"{dev.name}: actionControlSpeedControl: action = {action}")
        if dev.deviceTypeId != "leapFan":
            self.logger.warning(f"{dev.name}: actionControlSpeedControl: not a leapFan device")
            return

        bridge = self.leap_bridges.get(dev.pluginProps["bridge"], None)
        if not bridge:
            self.logger.warning(f"{dev.name}: actionControlSpeedControl: bridge not found")
            return

        if action.speedControlAction == indigo.kSpeedControlAction.TurnOn:
            last_speed = dev.pluginProps.get("last_speed", "Medium")
            self.event_loop.create_task(bridge.set_fan(dev.pluginProps["device"], last_speed))

        elif action.speedControlAction == indigo.kSpeedControlAction.TurnOff:
            newProps = dev.pluginProps
            newProps['last_speed'] = dev.states['fan_speed']    # save the last speed
            dev.replacePluginPropsOnServer(newProps)
            self.event_loop.create_task(bridge.set_fan(dev.pluginProps["device"], "Off"))

        elif action.speedControlAction == indigo.kSpeedControlAction.Toggle:
            if dev.onState:
                newProps = dev.pluginProps
                newProps['last_speed'] = dev.states['fan_speed']    # save the last speed
                dev.replacePluginPropsOnServer(newProps)
                self.event_loop.create_task(bridge.set_fan(dev.pluginProps["device"], "Off"))
            else:
                last_speed = dev.pluginProps.get("last_speed", "Medium")
                self.event_loop.create_task(bridge.set_fan(dev.pluginProps["device"], last_speed))

        elif action.speedControlAction == indigo.kSpeedControlAction.SetSpeedIndex:
            speed = _FAN_SPEED_MAP.get(action.actionValue, "Medium")
            self.logger.debug(f"{dev.name}: SetSpeedIndex to {action.actionValue}, speed = {speed}")
            self.event_loop.create_task(bridge.set_fan(dev.pluginProps["device"], speed))

        elif action.speedControlAction == indigo.kSpeedControlAction.IncreaseSpeedIndex:
            speed = _FAN_SPEED_MAP.get(min(dev.speedIndex + action.actionValue, 3))
            self.logger.debug(f"{dev.name}: IncreaseSpeedIndex by {action.actionValue}, speed = {speed}")
            self.event_loop.create_task(bridge.set_fan(dev.pluginProps["device"], speed))

        elif action.speedControlAction == indigo.kSpeedControlAction.DecreaseSpeedIndex:
            speed = _FAN_SPEED_MAP.get(max(dev.speedIndex - action.actionValue, 0))
            self.logger.debug(f"{dev.name}: DecreaseSpeedIndex by {action.actionValue}, speed = {speed}")
            self.event_loop.create_task(bridge.set_fan(dev.pluginProps["device"], speed))

    ########################################
    # Plugin Actions object callbacks (pluginAction is an Indigo plugin action instance)
    ########################################

    def activate_scene_action(self, pluginAction, bridge_dev):

        bridge = self.leap_bridges[bridge_dev.id]
        scene_id = pluginAction.props["scene_id"]
        self.logger.debug(f"{bridge_dev.name}: Activating scene {scene_id}")
        self.event_loop.create_task(bridge.activate_scene(scene_id))

    def tap_button_action(self, pluginAction, bridge_dev):

        bridge = self.leap_bridges[bridge_dev.id]
        button_address = pluginAction.props["button_address"]
        self.logger.debug(f"{bridge_dev.name}: Tapping button {button_address}")
        self.event_loop.create_task(bridge.tap_button(button_address.split(":")[1]))

    def fade_dimmer_action(self, pluginAction, dev):

        bridge = self.leap_bridges[dev.pluginProps["bridge"]]
        brightness = float(indigo.activePlugin.substitute(pluginAction.props["brightness"]))
        fadeTime = timedelta(seconds=float(indigo.activePlugin.substitute(pluginAction.props["fadeTime"])))
        self.logger.debug(f"{dev.name}: Fading to {brightness} over {fadeTime}")
        self.event_loop.create_task(bridge.set_value(dev.pluginProps["device"], brightness, fadeTime))

    def start_raising_action(self, pluginAction, dev):

        bridge = self.leap_bridges[dev.pluginProps["bridge"]]
        self.logger.debug(f"{dev.name}: Raising")
        self.event_loop.create_task(bridge.raise_cover(dev.pluginProps["device"]))

    def start_lowering_action(self, pluginAction, dev):

        bridge = self.leap_bridges[dev.pluginProps["bridge"]]
        self.logger.debug(f"{dev.name}: Lowering")
        self.event_loop.create_task(bridge.lower_cover(dev.pluginProps["device"]))

    def stop_shade_action(self, pluginAction, dev):

        bridge = self.leap_bridges[dev.pluginProps["bridge"]]
        self.logger.debug(f"{dev.name}: Stopping")
        self.event_loop.create_task(bridge.stop_cover(dev.pluginProps["device"]))

    def set_tilt_action(self, pluginAction, dev):

        bridge = self.leap_bridges[dev.pluginProps["bridge"]]
        tilt = float(indigo.activePlugin.substitute(pluginAction.props["tilt"]))
        self.logger.debug(f"{dev.name}: Tilting to {tilt}")
        self.event_loop.create_task(bridge.set_tilt(dev.pluginProps["device"], tilt))

    def set_fan_speed_action(self, pluginAction, dev):

        bridge = self.leap_bridges[dev.pluginProps["bridge"]]
        fan_speed = pluginAction.props["fan_speed"]
        self.logger.debug(f"{dev.name}: Setting fan speed: {fan_speed}")
        self.event_loop.create_task(bridge.set_fan(dev.pluginProps["device"], fan_speed))

    ########################################
    # This is the method that's called by the Add Linked Device button in the config dialog.
    ########################################

    def addLinkedDevice(self, valuesDict, typeId=None, devId=None):
        self.logger.debug(f"addLinkedDevice: valuesDict: {valuesDict}")

        buttonAddress = valuesDict["buttonDevice"]
        linked_device_id = valuesDict["linked_device"]
        linkName = valuesDict["linkName"]

        buttonDeviceID = self.keypads.get(buttonAddress, None)  # look in keypads first
        self.logger.debug(f"addLinkedDevice: buttonAddress: {buttonAddress}, buttonDeviceID: {buttonDeviceID}")

        if buttonDeviceID is None:
            buttonDeviceID = self.picos.get(buttonAddress, None)  # then look in picos
            self.logger.debug(f"addLinkedDevice: buttonAddress: {buttonAddress}, buttonDeviceID: {buttonDeviceID}")

        if buttonDeviceID is None:
            self.logger.error(f"addLinkedDevice: buttonAddress {buttonAddress} not found in keypads or picos")
            return

        self.logger.debug(f"addLinkedDevice: buttonAddress: {buttonAddress}, buttonDeviceID: {buttonDeviceID}")

        parts = buttonAddress.split(":")
        gatewayID = parts[0]
        parts = parts[1].split(".")
        deviceID = parts[0]
        componentID = parts[1]
        buttonLEDAddress = f"{gatewayID}:{deviceID}.{int(componentID) + 80}"
        try:
            buttonLEDDeviceId = self.keypads[buttonLEDAddress]
        except Exception as e:
            buttonLEDDeviceId = "0"
        linkID = f"{buttonDeviceID}-{linked_device_id}"
        if len(linkName) == 0:
            linkName = linkID
        linkItem = {"name": linkName, "buttonDevice": buttonDeviceID, "buttonLEDDevice": buttonLEDDeviceId, "controlledDevice": linked_device_id,
                    "buttonAddress": buttonAddress}
        self.logger.debug(f"Adding linkItem {linkID}: {linkItem}")
        self.linkedDeviceList[linkID] = linkItem
        self.logLinkedDevices()

        indigo.activePlugin.pluginPrefs["linkedDevices"] = json.dumps(self.linkedDeviceList)

    ########################################
    # This is the method that's called by the Delete Device button
    ########################################

    def deleteLinkedDevices(self, valuesDict, typeId=None, devId=None):

        for item in valuesDict["linkedDeviceList"]:
            self.logger.info(f"deleting device {item}")
            del self.linkedDeviceList[item]

        self.logLinkedDevices()
        indigo.activePlugin.pluginPrefs["linkedDevices"] = json.dumps(self.linkedDeviceList)

    def listLinkedDevices(self, filter="", valuesDict=None, typeId="", targetId=0):
        returnList = list()
        for linkID, linkItem in self.linkedDeviceList.items():
            returnList.append((linkID, linkItem["name"]))
        return sorted(returnList, key=lambda item: item[1])

    def logLinkedDevices(self):
        if len(self.linkedDeviceList) == 0:
            self.logger.info("No linked Devices")
            return

        fstring = "{:^25} {:^25} {:^20} {:^20} {:^20} {:^20}"
        self.logger.info(fstring.format("Link ID", "Link Name", "buttonDevice", "buttonLEDDevice", "controlledDevice", "buttonAddress"))
        for linkID, linkItem in self.linkedDeviceList.items():
            self.logger.info(
                fstring.format(linkID, linkItem["name"], linkItem["buttonDevice"], linkItem["buttonLEDDevice"], linkItem["controlledDevice"],
                               linkItem["buttonAddress"]))

    ########################################

    def menuDumpAll(self):
        self.logger.info(f"Bridge Data:\n{json.dumps(self.bridge_data, sort_keys=True, indent=4)}")
        return True

    def createDevicesMenu(self, valuesDict, typeId):
        self.event_loop.create_task(self.createBridgeDevices(valuesDict))
        return True

    ########################################

    async def createBridgeDevices(self, valuesDict):

        group_by = valuesDict["group_by"]
        rename_devices = bool(valuesDict["rename_devices"])
        self.logger.info(f"Creating Devices, Grouping = {group_by}")

        bridge_id = int(valuesDict["bridge"])
        if bridge_id not in self.bridge_data:
            self.logger.error(f"Bridge {bridge_id} not found, bridge_data =\n{self.bridge_data}")
            return

        bridgeData = self.bridge_data[bridge_id]

        for dev in bridgeData["devices"].values():
            self.logger.info(f"Device '{dev['name']}' ({dev['device_id']}), Area = {dev['area']}")

            if dev['type'] in ['SmartBridge', 'SmartBridge Pro']:
                continue    # skip the bridge itself

            try:
                areaName = bridgeData['areas'][dev['area']]['name']
            except KeyError:
                areaName = "Unknown"

            address = f"{bridge_id}:{dev['device_id']}"
            name = f"{dev['name']} ({dev['device_id']})"
            props = {
                "room": areaName,
                "bridge": bridge_id,
                "device": dev['device_id'],
            }
            if dev['type'] in LEAP_DEVICE_TYPES['light']:
                dev_type = DEV_DIMMER

            elif dev['type'] in LEAP_DEVICE_TYPES['switch']:
                dev_type = DEV_SWITCH

            elif dev['type'] in LEAP_DEVICE_TYPES['fan']:
                dev_type = DEV_FAN

            elif dev['type'] in LEAP_DEVICE_TYPES['cover']:
                dev_type = DEV_SHADE

            elif dev['type'] in LEAP_DEVICE_TYPES['sensor']:
                self.logger.debug(f"Skipping sensor device type {dev['type']}")
                continue

            else:
                self.logger.warning(f"Unknown device type {dev['type']}")
                continue

            self.createLutronDevice(dev_type, name, address, props, areaName, group_by, rename_devices)

        self.logger.info("Creating Devices done.")
        return

    def createLutronDevice(self, devType, name, address, props, area, group_by="None", rename_devices=False):

        self.logger.threaddebug(f"createLutronDevice: devType = {devType}, name = {name}, address = {address}, props = {props}, room = {area}")

        folderNameDict = {
            DEV_DIMMER: "Lutron Dimmers",
            DEV_SWITCH: "Lutron Switches",
            DEV_FAN: "Lutron Fans",
            DEV_SHADE: "Lutron Shades",
        }

        # first, make sure this device doesn't exist.  Unless I screwed up, the addresses should be unique
        # it would be more efficient to search through the internal device lists, but a pain to code.
        # If it does exist, update with the new properties

        for dev in indigo.devices.iter("self"):
            if dev.address == address:  # existing device
                self.logger.debug(f"Device: '{name}' ({address}) already exists")

                if rename_devices and dev.name != name:
                    self.logger.debug(f"Renaming '{dev.name}' to '{name}'")
                    dev.name = name
                    dev.replaceOnServer()

                return dev

        # Pick the folder for this device, create it if necessary

        if group_by == "Type":
            folderName = folderNameDict[devType]
            if folderName in indigo.devices.folders:
                theFolder = indigo.devices.folders[folderName].id
            else:
                self.logger.debug(f"Creating Device Folder: '{folderName}'")
                theFolder = indigo.devices.folder.create(folderName).id

        elif group_by == "Area":
            folderName = f"Lutron {area}"
            if folderName in indigo.devices.folders:
                theFolder = indigo.devices.folders[folderName].id
            else:
                self.logger.debug(f"Creating Device Folder: '{folderName}'")
                theFolder = indigo.devices.folder.create(folderName).id

        elif group_by == "None":
            folderName = "DEVICES"
            theFolder = 0

        else:
            self.logger.error("Unknown value for group_by")
            return

        # finally, create the device

        self.logger.info(f"Creating {devType} device: '{name}' ({address}) in '{folderName}'")
        try:
            newDevice = indigo.device.create(indigo.kProtocol.Plugin, address=address, name=name, deviceTypeId=devType, props=props, folder=theFolder)
        except Exception as e:
            self.logger.error(f"Error in indigo.device.create(): {e}")
            newDevice = None

        return newDevice
