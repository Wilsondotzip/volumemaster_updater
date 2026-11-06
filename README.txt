VolumeMaster Firmware Updater
=============================

This tool installs new firmware on your VolumeMaster device. It runs on Windows.
You don't need to install anything else or download a firmware file: the firmware
is included in the tool. Your VolumeMaster settings are stored on your PC and are
not changed.


BEFORE YOU START
----------------
- Plug the VolumeMaster directly into your PC if you can, rather than through a hub.
- Make sure the PC won't sleep or shut down, and keep a laptop on its charger.


HOW TO UPDATE
-------------
1. Unzip the download, then double-click VolumeMaster-Updater.exe.

   Windows may show "Windows protected your PC". If so, click "More info", then
   "Run anyway".

2. Check that "Device port" shows your device. It usually appears as
   "USB-SERIAL CH340". If you have more than one, choose the VolumeMaster.

3. Click "Update Firmware".

4. If VolumeMaster is open, you will be asked to close it. Click "Yes".

5. Wait for the update to finish. This should take about a minute or less.

   DO NOT unplug the device or turn off your PC during the update.

6. When you see "Update complete", close the updater and reopen VolumeMaster.


IF SOMETHING GOES WRONG
-----------------------
No port is listed, or your device isn't in the list
   Unplug the device and plug it back in, then open the list again. If it still
   doesn't appear, try another USB port or cable. You may also need to install the
   CH340 USB driver.

"Device not responding"
   The updater couldn't talk to the device.
   - If a previous update was interrupted or failed, click "Yes" to try again.
   - Otherwise, click "No", check that the right port is selected, then unplug the
     device, plug it back in and try again.

"Already up to date"
   The device already has this firmware, so you don't need to do anything. Click
   "No" to stop, or "Yes" to install it again.

"Replug the device"
   The new firmware has been installed, but the device needs a restart. Unplug it,
   plug it back in, then click "OK".

"Flashing failed"
   Do not unplug the device. Click "Update Firmware" to try again. You will probably
   see "Device not responding" next: click "Yes".

"Cannot open COM..."
   Another program is using the device. Close any other program that might be using
   it, such as a serial monitor, then try again.

The update was interrupted, or the device stopped working afterwards
   Unplug the device, plug it back in, then run the updater again and click "Yes" at
   "Device not responding". If that doesn't work, contact VolumeMaster support and
   include the text from the updater's log box.


LICENCE
-------
This tool includes STM32CubeProgrammer by STMicroelectronics. See
STM32CubeProgrammer-license.txt for its licence terms.
