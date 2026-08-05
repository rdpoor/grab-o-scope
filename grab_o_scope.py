#!/usr/bin/env python

"""
grab_o_scope: Capture contents of an oscilloscope screen to a .png file
"""
import argparse
import os
import platform
import pyvisa
import re
import subprocess
import sys

class Grabber:
    """Base class for oscilloscope screen grabbers.
    Each subclass should override IDN_PATTERN and capture_screen()
    """

    # Pattern to match the result of a '*IDN?' query
    IDN_PATTERN = r''

    @classmethod
    def capture_screen(cls, instrument):
        """Capture and return the screen data as a PNG image.
        Args:
            instrument (Resource): VISA resource representing the oscilloscope.
        Returns:
            list: Binary data of the oscilloscope screen in PNG format.
        """
        pass

class Keysight3000XGrabber(Grabber):
    """Grabber for the Keysight InfiniVision 3000T X-Series Oscilloscopes."""
    IDN_PATTERN = r'KEYSIGHT TECHNOLOGIES,.SO-X.*'

    @classmethod
    def capture_screen(cls, instrument):
        # Turn off ink saver to get non-inverted background image
        # (more info in "Programmer's Guide for InfiniiVision 3000T X-Series Oscilloscopes", page 426)
        instrument.write(':HARDcopy:INKSaver OFF')
        # Query the screen image as PNG, color
        buf = instrument.query_binary_values(':DISPlay:DATA? PNG,COLOR', datatype='B')
        return buf

class KeysightHD3Grabber(Grabber):
    """Grabber for the Keysight InfiniiVision HD3 Series Oscilloscopes."""
    IDN_PATTERN = r'KEYSIGHT TECHNOLOGIES,HD3.*'

    @classmethod
    def capture_screen(cls, instrument):
        # Query the screen image as PNG
        buf = instrument.query_binary_values(':DISPlay:DATA? PNG', datatype='B')
        return buf

class RigolDHO924Grabber(Grabber):
    """Grabber for the Rigol DHO924 oscilloscope."""
    IDN_PATTERN = r'RIGOL TECHNOLOGIES,DHO924,DHO.*'

    @classmethod
    def capture_screen(cls, instrument):
        buf = instrument.query_binary_values(':DISP:DATA? PNG', datatype='B')
        return buf

class RigolDS1054ZGrabber(Grabber):
    """Grabber for the Rigol DS1054Z oscilloscope."""
    IDN_PATTERN = r'RIGOL TECHNOLOGIES,DS1\w+Z,.*'

    @classmethod
    def capture_screen(cls, instrument):
        buf = instrument.query_binary_values(':DISP:DATA? ON,0,PNG', datatype='B')
        return buf

# ******************************************************************************
# Add device-specific subclasses of Grabber above this line.
# ******************************************************************************

class GrabOScope:
    """Main class for managing oscilloscope screen captures."""

    KNOWN_GRABBERS = [
        Keysight3000XGrabber,
        KeysightHD3Grabber,
        RigolDHO924Grabber,
        RigolDS1054ZGrabber,
    ]

    def __init__(self, options):
        """Initialize the GrabOScope with the specified options.
        Args:
            options (Namespace): Parsed command-line arguments.
        """
        self.options = options
        self._resource_manager = pyvisa.ResourceManager()

    def error_quit(self, message):
        """Print an error message and exit the program.

        Args:
            message (str): The error message to display.
        """
        sys.exit(f'{message}: ...quitting')

    def verbose_print(self, *args):
        """Print messages if verbose or trace mode is enabled.

        Args:
            *args: Variable arguments for the message to print.
        """
        if self.options.trace or self.options.verbose:
            print(*args, flush=True)

    def trace_print(self, *args):
        """Print debugging messages if trace mode is enabled.

        Args:
            *args: Variable arguments for the message to print.
        """
        if self.options.trace:
            print(*args, flush=True)

    def list_instrument_names(self):
        """List all connected instruments.

        Returns:
            list: Names of connected instruments.
        """
        self.verbose_print('Searching for instruments...')
        instrument_names = self._resource_manager.list_resources()
        self.trace_print(f'Found instruments: {instrument_names}')
        return instrument_names

    def get_idn_string(self, instrument_name):
        """Get the result of an '*IDN?' query for a given instrument.

        Args:
            instrument_name (str): Name of the instrument to query.

        Returns:
            str: The IDN string if successful, or None if the query fails.
        """
        try:
            instrument = self._resource_manager.open_resource(instrument_name)
            idn_string = instrument.query('*IDN?')
            self.trace_print(f'{instrument_name} => {idn_string.strip()}')
            instrument.close()
        except pyvisa.errors.VisaIOError:
            self.trace_print(f'{instrument_name} => None')
            return None
        return idn_string

    def find_grabber(self, idn_string):
        """Find a grabber class that matches the given IDN string.

        Args:
            idn_string (str): IDN string of an instrument.

        Returns:
            Grabber: The appropriate grabber class, or None if no match is found.
        """
        for grabber_cls in self.KNOWN_GRABBERS:
            if hasattr(grabber_cls, 'IDN_PATTERN') and re.search(grabber_cls.IDN_PATTERN, idn_string):
                return grabber_cls
        return None

    def find_grabbers(self, instrument_names, target_name):
        """Find all matching grabbers for connected instruments.

        Uses a two pass approach:
        1. First tries fast filtering by VISA resource name (for IPs, resource types)
        2. Falls back to querying all devices if needed (for model names)

        Args:
            instrument_names (list): List of instrument names.
            target_name (str): Specific instrument name to target (can be part of resource name, IP, or model name).

        Returns:
            list: List of dictionaries containing matched grabber classes and instrument details.
        """
        grabbers = []
        
        # Determine search strategy based on target_name
        if target_name is not None:
            # Check if target_name looks like it could match a VISA resource name
            # (contains IP indicators, VISA keywords, or is purely numeric)
            resource_indicators = ['.', ':', 'TCPIP', 'GPIB', 'USB', 'ASRL']
            target_upper = target_name.upper()
            
            # Check if purely numeric (likely an IP address) or contains resource indicators
            is_numeric = target_name.replace('.', '').replace(':', '').isdigit()
            has_resource_indicator = any(indicator in target_upper for indicator in resource_indicators)
            looks_like_resource = is_numeric or has_resource_indicator
            
            if looks_like_resource:
                # Fast path: Filter instrument list first (for IPs and resource names) case-insensitive match
                target_lower = target_name.lower()
                filtered_names = [n for n in instrument_names if target_lower in n.lower()]
                if filtered_names:
                    self.trace_print(f'Fast filtering: {len(instrument_names)} instruments -> {len(filtered_names)} matching "{target_name}"')
                    instrument_names = filtered_names
                else:
                    # No match in resource names, fall back to full search
                    self.trace_print(f'No resource name matches for "{target_name}", searching all devices by IDN...')
            else:
                # Need to query all devices
                self.trace_print(f'"{target_name}" appears to be a model name, querying all {len(instrument_names)} devices...')
        
        self.verbose_print('Searching for known oscilloscopes...')
        for instrument_name in instrument_names:
            idn_string = self.get_idn_string(instrument_name)
            if idn_string is not None:
                grabber = self.find_grabber(idn_string)
                if grabber is not None:
                    # Check if this oscilloscope matches the target_name filter
                    matches = True
                    if target_name is not None:
                        # Search in both the VISA resource name and the IDN string (case-insensitive)
                        target_lower = target_name.lower()
                        matches = (target_lower in instrument_name.lower()) or (target_lower in idn_string.lower())
                    
                    if matches:
                        grabbers.append({
                            'cls': grabber,
                            'name': instrument_name,
                            'idn': idn_string
                        })
        self.verbose_print(f'Found oscilloscopes: {grabbers}')
        return grabbers

    def find_singleton_grabber(self):
        """Find a single connected oscilloscope, or prompt if multiple are found.

        Returns:
            dict: Information about the identified oscilloscope grabber.
        """
        instrument_names = self.list_instrument_names()
        grabbers = self.find_grabbers(instrument_names, self.options.name)
        if len(grabbers) == 0:
            self.error_quit('Could not find any known oscilloscopes')
        elif len(grabbers) > 1:
            print('Multiple oscilloscopes found:')
            for grabber in grabbers:
                print(f'   {grabber["name"]}')
            self.error_quit('Please re-run with --name to select one of the above')
        else:
            return grabbers[0]

    def get_screen_bytes(self, grabber):
        """Retrieve the screen data from the oscilloscope.

        Args:
            grabber (dict): Dictionary containing the grabber class and instrument name.

        Returns:
            list: Binary data of the oscilloscope screen in PNG format.
        """
        name = grabber['name']
        try:
            instrument = self._resource_manager.open_resource(name)
            instrument.timeout = 10000
            buf = grabber['cls'].capture_screen(instrument)
            instrument.close()
            return buf
        except pyvisa.errors.VisaIOError:
            self.error_quit(f'Could not read bytes from {grabber["name"]}')
            return None

    def grab(self):
        """Capture the oscilloscope screen and save it as a file."""
        grabber = self.find_singleton_grabber()
        filename = self.options.filename or 'grab-o-scope.png'
        self.verbose_print(f'Writing screen capture to {filename}')

        buf = self.get_screen_bytes(grabber)
        with open(filename, 'wb') as f:
            self.verbose_print(f'Captured screen, writing {len(buf)} bytes to {filename}')
            f.write(bytearray(buf))

        if self.options.auto_view:
            self.view_file(filename)

    @classmethod
    def view_file(cls, filename):
        """Open a file with the native viewer for the operating system.

        Args:
            filename (str): Path to the file to open.
        """
        if platform.system() == 'Darwin':
            subprocess.call(('open', filename))
        elif platform.system() == 'Windows':
            os.startfile(filename)
        else:
            subprocess.call(('xdg-open', filename))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Oscilloscope Screen Grabber')
    parser.add_argument('-n', '--name', default=None, help='name a specific instrument')
    parser.add_argument('-f', '--filename', default='grab-o-scope.png', help='name of output file')
    parser.add_argument('-a', '--auto_view', action='store_true', help='automatically view output file')
    parser.add_argument('-v', '--verbose', action='store_true', help='print additional output')
    parser.add_argument('-t', '--trace', action='store_true', help='include debugging output')
    opts = parser.parse_args()

    GrabOScope(opts).grab()
