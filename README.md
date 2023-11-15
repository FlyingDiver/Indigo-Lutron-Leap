# Indigo-Lutron-Leap
Plugin to support Lutron systems using the LEAP protocol.  

Supports Bridges:

* Lutron Caséta Smart Hub (L-BDG2-WH)
* Lutron Caséta Smart Bridge PRO (L-BDGPRO2-WH)
* RadioRA 2 Select Main Repeater (RR-SEL-REP2-BL)
* RadioRA 3 All-in-One Processor (RR-PROC3)
* Homeworks QSX Processor (HQP7)

There are some systems that support both the LEAP and the original protocol. If you have one of these,
you should use the original plugin. The LEAP protocol is not as complete as the original protocol at this time.

However, if you need both because you have a mix of old and new hardware, both plugins can
be used simultaneously. The LEAP plugin will not interfere with the original plugin.  You can migrate devices 
from the original plugin to the LEAP plugin at any time.  You could even have some devices in both plugins
at the same time (but that's not recommended except for the migration process).

Supported Devices:

* Wall and plug-in dimmers
* Wall Switches
* Shades
* Fans
* Occupancy/Vacancy Sensors
* Pico Remotes

As of the current release only the following devices have been tested:

* RadioRa 3 All-in-One Processor (RR-PROC3)
* RadioRa 3 Dimmers (RRST-PRO-NFB-XX, etc)
* RadioRa 3 Keypads (RRST-W4B-XX, etc)
* RadioRa 2 Dimmers (RRD-6ND, etc)
* Lutron Caséta Smart Hub
* Lutron Caséta Smart Bridge PRO
* Occupancy/Vacancy Sensors
* Caseta plug-in dimmers
* Pico Remotes
* Caseta Fan Controllers

* Other devices that are supported by the LEAP protocol should work, but have not been tested.

Note that Keypad buttons, Pico Remotes and the Occupancy sensors do not exist as Indigo devices. They can only be used as triggers 
for action groups. These devices have no "state", so there are no devices for them.

Currently the RRa3 Keypad LEDs are supported as Indigo switch devices, but cannot be controller if they are
programmed as device or scene controls in the Lutron software.  The Lutron scene settings will override any
changes made in Indigo.  If you want to control the LEDs from Indigo, the buttons cannot be programmed at all.

Before enabling the plugin, you need to make sure the zeroconf package is installed. You can do this from the command line:
````
pip3 install zeroconf
````
When the plugin starts up, it uses zero-conf (aka Bonjour) to get a list of Lutron bridge-type devices on your LAN.

Not all discovered devices work with the plugin (the RRa2 Connect Bridge does not, for example). 

Create a"Lutron Bridge" plugin device, and select your bridge from the popup. If nothing shows up, make sure the device is 
online (you can see it in the Lutron mobile app). You can enable detailed debugging to see if the discovery 
process is doing anything.

When you create the Bridge device, you'll be prompted to pair the plugin with the bridge. You need to press 
the pairing button on the device. On the Caseta hub, it's a button next to the power connector.
On the RRa3 processor, it's a very small button on the front of the device near the status LED.

You should see something like this in the Indigo log:
````
Lutron Leap                     Starting pairing process for Bridge Lutron-05124472.local., please press the pairing button on the Bridge.
Lutron Leap                     Pairing complete: Bridge version is 1.115.
````

Once that's complete, use the menu command to "Create Devices from Bridge". You cannot manually create the devices, only the auto-create method is supported.

For other Lutron systems that use the original Lutron integration protocol, especially RadioRA 2, 
see the original Lutron plugin at https://www.indigodomo.com/pluginstore/84/