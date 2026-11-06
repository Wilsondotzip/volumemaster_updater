"""VolumeMaster firmware updater (STM32C011 behind a CH340 USB-serial bridge).

    1. Asks the user to close VolumeMaster, whose backend holds the COM port.
    2. Sends ENTER_BOOTLOADER to the running firmware (9600 8N1), which jumps to
       ST's ROM bootloader on the same USART.
    3. Flashes and verifies with the bundled STM32_Programmer_CLI.
    4. Asks the new firmware for its version with V: to confirm it is running.

The firmware's serial commands are '\\n'-terminated lines; '\\r' is ignored.
"""
import glob
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import serial
from serial.tools import list_ports

APP_BAUD = 9600
BOOTLOADER_BAUD = 115200

FLASH_START = 0x08000000
FLASH_SIZE = 32 * 1024
RAM_START = 0x20000000
RAM_SIZE = 6 * 1024

CH340 = (0x1A86, 0x7523)

# Exact names, so this never matches the updater itself or unrelated plugins.
APP_PROCESSES = ('VolumeMaster.exe', 'VolumeMaster-Headless.exe', 'VolumeMaster-Pro-Headless.exe')

CLI_TIMEOUT = 180  # seconds; a normal flash takes well under a minute
BOOT_WAIT = 8      # seconds to wait for the new firmware after the CLI's reset
REPLUG_WAIT = 60   # seconds to wait after asking the user to replug the device

VERSION_LINE = re.compile(r'\d+(?:\.\d+)+')
VERSION_IN_IMAGE = re.compile(rb'V:\x00+(\d+(?:\.\d+)+)\x00')

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


class UpdateError(Exception):
    """A failure with a message that is meant to be shown to the user."""


def base_dir():
    """Where bundled files live: PyInstaller's unpack dir, or next to this script."""
    return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))


def cli_path():
    return os.path.join(base_dir(), 'vendor', 'stm32programmer', 'bin', 'STM32_Programmer_CLI.exe')


def bundled_firmware():
    """The one image in firmware/. More than one is a packaging mistake, so it never guesses."""
    found = glob.glob(os.path.join(base_dir(), 'firmware', '*.bin'))
    if len(found) != 1:
        raise UpdateError(f'This updater was built with {len(found)} firmware files instead of 1.')
    return found[0]


def read_image(path):
    """Sanity-checks a firmware file. Returns its version string, or None if it has none."""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError as e:
        raise UpdateError(f'Cannot read the firmware file:\n{e}') from e

    if len(data) < 8 or len(data) > FLASH_SIZE:
        raise UpdateError(f'{os.path.basename(path)} is {len(data)} bytes, which does not fit this device.')
    stack, reset = struct.unpack('<II', data[:8])
    if not RAM_START < stack <= RAM_START + RAM_SIZE or not FLASH_START < reset < FLASH_START + len(data):
        raise UpdateError(f'{os.path.basename(path)} is not a VolumeMaster firmware image.')
    # Without this command the device could never be updated over serial again.
    if b'ENTER_BOOTLOADER' not in data:
        raise UpdateError(f'{os.path.basename(path)} does not support serial updates, so it was not flashed.')

    match = VERSION_IN_IMAGE.search(data)
    return match.group(1).decode() if match else None


def running_app_processes():
    output = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True,
                            errors='replace', creationflags=NO_WINDOW).stdout
    names = {line.split('","')[0].strip('"').lower() for line in output.splitlines()}
    return [p for p in APP_PROCESSES if p.lower() in names]


def close_app(ui):
    """Asks to close VolumeMaster if it is running. Raises UpdateError if it stays open."""
    if not running_app_processes():
        return
    if not ui.ask('Close VolumeMaster?',
                  'VolumeMaster is running and is using the device, so it needs to close '
                  'before updating.\n\nClose VolumeMaster now?'):
        raise UpdateError('Update cancelled. VolumeMaster is still running.')

    ui.log('Closing VolumeMaster...')
    # The main app goes first: it relaunches a backend that dies on its own.
    for name in APP_PROCESSES:
        subprocess.run(['taskkill', '/F', '/T', '/IM', name], capture_output=True, creationflags=NO_WINDOW)

    deadline = time.monotonic() + 5
    while running_app_processes():
        if time.monotonic() > deadline:
            raise UpdateError('VolumeMaster did not close. Close it from the system tray and try again.')
        time.sleep(0.25)


def open_port(port):
    """Opens the device at the firmware's settings, retrying while a closed app releases it."""
    deadline = time.monotonic() + 3
    while True:
        try:
            return serial.Serial(port, APP_BAUD, timeout=0.2, write_timeout=1)
        except serial.SerialException as e:
            if time.monotonic() > deadline:
                raise UpdateError(f'Cannot open {port}. Close any program using it '
                                  f'(serial monitors, PuTTY, etc.) and try again.\n\n{e}') from e
            time.sleep(0.25)


def query_version(ser, seconds=1.5):
    """Sends V: and returns the reply, skipping any knob readings in between. None if no reply."""
    ser.reset_input_buffer()
    # The leading newline clears anything left in the firmware's line buffer.
    ser.write(b'\nV:\n')
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        line = ser.readline().decode('ascii', errors='replace').strip()
        if VERSION_LINE.fullmatch(line):
            return line
    return None


def enter_bootloader(port, image_version, ui):
    """Puts the device into the bootloader, unless it does not answer and the user stops here."""
    ser = open_port(port)
    try:
        version = query_version(ser)
        if version is None:
            # A device left blank by an interrupted update boots straight to the
            # bootloader, and would look exactly like this.
            if not ui.ask('Device not responding',
                          f'No VolumeMaster firmware answered on {port}.\n\nThis is normal if a previous '
                          'update was interrupted. Otherwise, the wrong port may be selected.\n\n'
                          'Try to update anyway?'):
                raise UpdateError('Update cancelled.')
            ui.log('No firmware answered. Trying the bootloader directly.')
            return None

        ui.log(f'Device is running firmware {version}.')
        if version == image_version and not ui.ask(
                'Already up to date', f'The device is already on firmware {version}.\n\nFlash it again anyway?'):
            raise UpdateError('Update cancelled.')

        ui.log('Entering bootloader...')
        ser.write(b'ENTER_BOOTLOADER\n')
        ser.flush()
        # The firmware waits 50 ms after replying before it jumps.
        ser.readline()
        time.sleep(0.2)
        return version
    finally:
        ser.close()


def flash(port, path, ui):
    """Erases, writes, verifies and resets. Output is logged as the CLI's own record of events."""
    ui.log('Flashing firmware. Do not unplug the device...')
    try:
        result = subprocess.run(
            [cli_path(), '-c', f'port={port}', f'br={BOOTLOADER_BAUD}', 'P=EVEN',
             '-w', path, hex(FLASH_START), '-v', '-rst', '-q'],
            capture_output=True, text=True, errors='replace', timeout=CLI_TIMEOUT, creationflags=NO_WINDOW)
    except FileNotFoundError as e:
        raise UpdateError(f'The STM32 programmer is missing from this install:\n{cli_path()}') from e
    except subprocess.TimeoutExpired as e:
        raise UpdateError('The programmer stopped responding. Do not unplug the device. '
                          'Close this window, reopen it and try again.') from e

    for line in result.stdout.splitlines():
        if line.strip():
            ui.log(line.rstrip())

    # The exit code alone is not enough: it also covers the reset, which can fail
    # after a good flash. The new firmware's version check below catches that case.
    if 'Download verified successfully' not in result.stdout:
        raise UpdateError('Flashing failed. The device has not been updated.\n\n'
                          'Do not unplug it. Try again, and see the log for details.')
    ui.log('Flash verified.')


def wait_for_version(port, seconds):
    """Polls the device until its firmware answers V:. The port may vanish during a replug."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with serial.Serial(port, APP_BAUD, timeout=0.2, write_timeout=1) as ser:
                version = query_version(ser, seconds=1)
                if version:
                    return version
        except (serial.SerialException, OSError):
            time.sleep(0.5)
    return None


def confirm_running(port, image_version, ui):
    """Checks that the new firmware has actually started. Returns the version it reports."""
    ui.log('Waiting for the new firmware to start...')
    version = wait_for_version(port, BOOT_WAIT)
    if version is None:
        if not ui.ask('Replug the device',
                      'The firmware was written and verified, but the device needs a restart.\n\n'
                      'Unplug the device, plug it back in, then click OK.', kind='okcancel'):
            raise UpdateError('The firmware was written, but it has not been confirmed running. '
                              'Unplug the device and plug it back in.')
        version = wait_for_version(port, REPLUG_WAIT)
    if version is None:
        raise UpdateError('The firmware was written and verified, but the device did not answer afterwards.')
    if image_version and version != image_version:
        raise UpdateError(f'The device reports firmware {version}, but {image_version} was flashed.')
    return version


def run_update(port, path, ui):
    """The whole update. Returns the version now running. Raises UpdateError on failure."""
    image_version = read_image(path)
    ui.log(f'Firmware file: {os.path.basename(path)} (version {image_version or "unknown"})')
    close_app(ui)
    enter_bootloader(port, image_version, ui)
    flash(port, path, ui)
    return confirm_running(port, image_version, ui)


def list_com_ports():
    """Returns [(device, label)], with CH340 adapters (the VolumeMaster's bridge) first."""
    ports = sorted(list_ports.comports(), key=lambda p: ((p.vid, p.pid) != CH340, p.device))
    return [(p.device, f'{p.device} - {p.description}') for p in ports]


class UpdaterWindow:
    """The window. The update runs on a worker thread, which only touches Tk through _calls."""

    def __init__(self, root):
        self.root = root
        self._calls = queue.Queue()
        self._busy = False
        self._ports = []

        root.title('VolumeMaster Firmware Updater')
        root.geometry('560x400')
        root.minsize(460, 320)
        root.protocol('WM_DELETE_WINDOW', self._on_close)

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)

        ttk.Label(frame, text='Device port').grid(row=0, column=0, sticky='w', padx=(0, 8), pady=4)
        self.port_box = ttk.Combobox(frame, state='readonly', postcommand=self._refresh_ports)
        self.port_box.grid(row=0, column=1, sticky='ew', pady=4)

        ttk.Label(frame, text='Firmware').grid(row=1, column=0, sticky='w', padx=(0, 8), pady=4)
        self.firmware_path, firmware_text = self._load_firmware()
        ttk.Label(frame, text=firmware_text, wraplength=420).grid(row=1, column=1, sticky='w', pady=4)

        self.update_button = ttk.Button(frame, text='Update Firmware', command=self._start)
        self.update_button.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(8, 4))
        if not self.firmware_path:
            self.update_button['state'] = 'disabled'

        self.progress = ttk.Progressbar(frame, mode='indeterminate')
        self.progress.grid(row=3, column=0, columnspan=2, sticky='ew', pady=4)

        log_frame = ttk.Frame(frame)
        log_frame.grid(row=4, column=0, columnspan=2, sticky='nsew', pady=(4, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_box = tk.Text(log_frame, height=10, wrap='word', state='disabled')
        self.log_box.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(log_frame, command=self.log_box.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.log_box['yscrollcommand'] = scroll.set

        self._refresh_ports()
        if self._ports:
            self.port_box.current(0)
        self.root.after(50, self._drain_calls)

    def _load_firmware(self):
        """Returns (path, text to show), with a path of None if the bundled image is unusable."""
        try:
            path = bundled_firmware()
            return path, f'Version {read_image(path) or "unknown"} ({os.path.basename(path)})'
        except UpdateError as e:
            return None, str(e)

    def _refresh_ports(self):
        selected = self._selected_port()
        self._ports = list_com_ports()
        self.port_box['values'] = [label for _, label in self._ports]
        devices = [device for device, _ in self._ports]
        if selected in devices:
            self.port_box.current(devices.index(selected))
        else:
            self.port_box.set('')

    def _selected_port(self):
        index = self.port_box.current()
        return self._ports[index][0] if 0 <= index < len(self._ports) else None

    def _drain_calls(self):
        while not self._calls.empty():
            self._calls.get()()
        self.root.after(50, self._drain_calls)

    def _on_close(self):
        # Closing mid-flash would kill the programmer and leave the device half-written.
        if self._busy:
            messagebox.showwarning('Update in progress', 'Please wait for the update to finish.', parent=self.root)
        else:
            self.root.destroy()

    def _set_busy(self, busy):
        self._busy = busy
        self.update_button['state'] = 'disabled' if busy else 'normal'
        self.port_box['state'] = 'disabled' if busy else 'readonly'
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _start(self):
        port = self._selected_port()
        if not port:
            messagebox.showerror('No device', 'Plug in the device and choose its port.', parent=self.root)
            return
        self._set_busy(True)
        threading.Thread(target=self._worker, args=(port, self.firmware_path), daemon=True).start()

    def _worker(self, port, path):
        try:
            version = run_update(port, path, self)
            self.log(f'Update complete. The device is running firmware {version}.')
            self._notify(messagebox.showinfo, 'Update complete',
                         f'The device is now running firmware {version}.\n\nYou can reopen VolumeMaster.')
        except UpdateError as e:
            self.log(f'Stopped: {e}')
            self._notify(messagebox.showerror, 'Update stopped', str(e))
        except Exception as e:
            self.log(f'Unexpected error: {e!r}')
            self._notify(messagebox.showerror, 'Update stopped', f'Unexpected error:\n{e}')
        finally:
            self._calls.put(lambda: self._set_busy(False))

    def _notify(self, dialog, title, message):
        self._calls.put(lambda: dialog(title, message, parent=self.root))

    def log(self, message):
        """Thread-safe: appends a line to the log box."""
        def append():
            self.log_box['state'] = 'normal'
            self.log_box.insert('end', message + '\n')
            self.log_box.see('end')
            self.log_box['state'] = 'disabled'
        self._calls.put(append)

    def ask(self, title, message, kind='yesno'):
        """Thread-safe: shows a popup and blocks the worker until the user answers."""
        dialog = messagebox.askokcancel if kind == 'okcancel' else messagebox.askyesno
        answer = []
        answered = threading.Event()

        def show():
            answer.append(dialog(title, message, parent=self.root))
            answered.set()
        self._calls.put(show)
        answered.wait()
        return answer[0]


def main():
    root = tk.Tk()
    UpdaterWindow(root)
    root.mainloop()


if __name__ == '__main__':
    main()
