#!/usr/bin/env python
#
# Copyright 2014 Matthew Wall
# Copyright 2014 Nate Bargmann <n0nb@n0nb.us>
# See the file LICENSE.txt for your rights.
#
# Credit to and contributions from:
#   Jay Nugent (WB8TKL) and KRK6 for weather-2.kr6k-V2.1
#     http://server1.nuge.com/~weather/
#   Steve (sesykes71) for testing the first implementations of this driver
#   Garret Power for improved decoding and proper handling of negative values
#   Chris Thompstone for testing the fast-read implementation
#
# Thanks to PeetBros for publishing the communication protocols and details
# about each model they manufacture.

"""Driver for Peet Bros Ultimeter weather stations except the Ultimeter II

This driver assumes the Ultimeter is emitting data in Peet Bros Data Logger
mode format.  This driver will set the mode automatically on stations
manufactured after 2004.  Stations manufactured before 2004 must be set to
data logger mode using the buttons on the console.

Resources for the Ultimeter stations

Ultimeter Models 2100, 2000, 800, & 100 serial specifications:
  http://www.peetbros.com/shop/custom.aspx?recid=29

Ultimeter 2000 Pinouts and Parsers:
  http://www.webaugur.com/ham-radio/52-ultimeter-2000-pinouts-and-parsers.html

Ultimeter II
  not supported by this driver

All models communicate over an RS-232 compatible serial port using three
wires--RXD, TXD, and Ground (except Ultimeter II which omits TXD).  Port
parameters are 2400, 8N1, with no flow control.

The Ultimeter hardware supports several "modes" for providing station data
to the serial port.  This driver utilizes the "modem mode" to set the date
and time of the Ultimeter upon initialization and then sets it into Data
Logger mode for continuous updates.

Modem Mode commands used by the driver
    >Addddmmmm  Set Date and Time (decimal digits dddd = day of year,
                mmmm = minute of day; Jan 1 = 0000, Midnight = 0000)

    >I          Set output mode to Data Logger Mode (continuous output)

See http://www.peetbros.com/shop/custom.aspx?recid=29

CHANGE HISTORY
--------------------------------
0.20 02/13/2019
Changed from using serial.readline() to serial.read().
Ported to Python 3

0.21 02/21/2019
Now uses serial.inWaiting() to get the number of  bytes to be read,
instead of using a fixed number.

0.22 02/21/2019
Read a whole buffer full of bytes, but throw away any partial packets.

0.23 02/21/2019
Added debug info when no bytes are received.

0.24 02/26/2019
Read byte-by-byte.
"""

from __future__ import with_statement
from __future__ import print_function

import serial
import syslog
import time

import weewx.drivers
import weewx.wxformulas
from weewx.units import INHG_PER_MBAR, MILE_PER_KM
from weeutil.weeutil import timestamp_to_string, GenWithPeek

DRIVER_NAME = 'Ultimeter'
DRIVER_VERSION = '0.24'


def loader(config_dict, _):
    return UltimeterDriver(**config_dict[DRIVER_NAME])


def confeditor_loader():
    return UltimeterConfEditor()


def logmsg(level, msg):
    syslog.syslog(level, 'ultimeter: %s' % msg)


def logdbg(msg):
    logmsg(syslog.LOG_DEBUG, msg)


def loginf(msg):
    logmsg(syslog.LOG_INFO, msg)


def logerr(msg):
    logmsg(syslog.LOG_ERR, msg)


def _fmt(x):
    return ' '.join([b"%0.2X" % ord(c) for c in x])


class UltimeterDriver(weewx.drivers.AbstractDevice):
    """WeeWX driver that communicates with a Peet Bros Ultimeter station"""

    def __init__(self, **stn_dict):
        """Initialize an instance of UltimeterDriver

            model: station model, e.g., 'Ultimeter 2000' or 'Ultimeter 100'
            [Optional. Default is 'Ultimeter']

            port - serial port
            [Optional. Default is /dev/ttyUSB0]

            max_tries - how often to retry serial communication before giving up
            [Optional. Default is 5]

            retry_wait -- After an error, how long to wait before retrying.
            [Optional. Default is 3]

            debug_serial: Set to non-zero for extra debugging info about the serial port.
            [Optional. Default is 0]
        """
        self.model = stn_dict.get('model', 'Ultimeter')
        self.port = stn_dict.get('port', Station.DEFAULT_PORT)
        self.max_tries = int(stn_dict.get('max_tries', 5))
        self.retry_wait = int(stn_dict.get('retry_wait', 3))
        debug_serial = int(stn_dict.get('debug_serial', 0))
        self.last_rain = None

        loginf('driver version is %s' % DRIVER_VERSION)
        loginf('using serial port %s' % self.port)
        self.station = Station(self.port, debug_serial=debug_serial)
        self.station.open()

    def closePort(self):
        if self.station:
            self.station.close()
            self.station = None

    @property
    def hardware_name(self):
        return self.model

    def DISABLED_getTime(self):
        return self.station.get_time()

    def DISABLED_setTime(self):
        self.station.set_time(int(time.time()))

    def genLoopPackets(self):
        self.station.set_logger_mode()
        while True:
            packet = {'dateTime': int(time.time() + 0.5),
                      'usUnits': weewx.US}
            readings = self.station.get_readings_with_retry(self.max_tries,
                                                            self.retry_wait)
            data = Station.parse_readings(readings)
            packet.update(data)
            self._augment_packet(packet)
            yield packet

    def _augment_packet(self, packet):
        packet['rain'] = weewx.wxformulas.calculate_rain(
            packet['rain_total'], self.last_rain)
        self.last_rain = packet['rain_total']


class Station(object):
    DEFAULT_PORT = '/dev/ttyUSB0'

    def __init__(self, port, debug_serial=0, retry_read=0.5):
        self.port = port
        self._debug_serial = debug_serial
        self.retry_read = retry_read  # Seconds
        self.baudrate = 2400
        self.timeout = 3  # seconds
        self.serial_port = None
        # setting the year works only for models 2004 and later
        self.can_set_year = True
        # modem mode is available only on models 2004 and later
        # not available on pre-2004 models 50/100/500/700/800
        self.has_modem_mode = True

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, _, value, traceback):
        self.close()

    def open(self):
        logdbg("open serial port %s" % self.port)
        self.serial_port = serial.Serial(self.port, self.baudrate,
                                         timeout=self.timeout)

    def close(self):
        if self.serial_port is not None:
            logdbg("close serial port %s" % self.port)
            self.serial_port.close()
            self.serial_port = None

    def get_time(self):
        try:
            self.set_logger_mode()
            buf = self.get_readings_with_retry()
            data = Station.parse_readings(buf)
            d = data['day_of_year']  # seems to start at 0
            m = data['minute_of_day']  # 0 is midnight before start of day
            tt = time.localtime()
            y = tt.tm_year
            s = tt.tm_sec
            ts = time.mktime((y, 1, 1, 0, 0, s, 0, 0, -1)) + d * 86400 + m * 60
            logdbg("station time: day:%s min:%s (%s)" %
                   (d, m, timestamp_to_string(ts)))
            return ts
        except (serial.serialutil.SerialException, weewx.WeeWxIOError) as e:
            logerr("get_time failed: %s" % e)
        return int(time.time())

    def set_time(self, ts):
        # go to modem mode so we do not get logger chatter
        self.set_modem_mode()

        # set time should work on all models
        tt = time.localtime(ts)
        cmd = b">A%04d%04d" % (tt.tm_yday - 1, tt.tm_min + tt.tm_hour * 60)
        logdbg("set station time to %s (%s)" % (timestamp_to_string(ts), cmd))
        self.serial_port.write(b"%s\r" % cmd)

        # year works only for models 2004 and later
        if self.can_set_year:
            cmd = b">U%s" % tt.tm_year
            logdbg("set station year to %s (%s)" % (tt.tm_year, cmd))
            self.serial_port.write(b"%s\r" % cmd)

    def set_logger_mode(self):
        # in logger mode, station sends logger mode records continuously
        if self._debug_serial:
            logdbg("set station to logger mode")
        self.serial_port.write(b">I\r")

    def set_modem_mode(self):
        # setting to modem mode should stop data logger output
        if self.has_modem_mode:
            if self._debug_serial:
                logdbg("set station to modem mode")
            self.serial_port.write(b">\r")

    def get_readings_with_retry(self, max_tries=5, retry_wait=3):
        """Return a packet, retrying as necessary"""
        for ntries in range(max_tries):
            try:
                buf = self.get_readings()
                self.validate_buffer(buf)
                return buf
            except (serial.serialutil.SerialException, weewx.WeeWxIOError) as e:
                loginf("Failed attempt %d of %d to get readings: %s" %
                       (ntries + 1, max_tries, e))
                time.sleep(retry_wait)
        else:
            msg = "Max retries (%d) exceeded for readings" % max_tries
            logerr(msg)
            raise weewx.RetriesExceeded(msg)

    def get_readings(self):
        """Return a single packet. The packet will start with a double exclamation point,
        and end with \r\n"""

        # Wrap the raw bytes generator with a GenWithPeak generator, which allows us
        # to peek at the next byte
        gen_bytes = GenWithPeek(self._gen_bytes_raw())

        # Start by throwing away any partial packets:
        for ibyte in gen_bytes:
            if ibyte == b'!' and gen_bytes.peek() == b'!':
                break

        # Save the first exclamation point
        buf = ibyte
        # Now march through the bytes
        for ibyte in gen_bytes:
            # Add the new byte
            buf += ibyte
            # If this is a \r and the next character is \n, then we've reached the
            # end of a line
            if ibyte == b'\r' and gen_bytes.peek() == b'\n':
                # Add the \n
                buf += next(gen_bytes)
                if self._debug_serial:
                    logdbg("station said: %s" % _fmt(buf))
                return buf

    def _gen_bytes_raw(self):
        """Generator function that yields raw bytes."""
        while True:
            # Read byte-by-byte. Anything else seems to induce memory leaks...
            b = self.serial_port.read(1)
            # Did we get anything?
            if len(b):
                # Yes. Return it
                yield b
            else:
                # No. Sleep a bit, then try again
                logdbg("No bytes available. Sleeping %.1f seconds" % self.retry_read)
                time.sleep(self.retry_read)

    def validate_buffer(self, buf):
        """Validate that the buffer is in canonical form"""
        if len(buf) not in [44, 48, 52]:
            raise weewx.WeeWxIOError("Unexpected buffer length %d" % len(buf))
        if buf[0:2] != b'!!':
            raise weewx.WeeWxIOError("Unexpected header bytes '%s'" % buf[0:2])
        if buf[-2:] != b'\r\n':
            raise weewx.WeeWXIOError("Unexpected tail bytes '%s'" % buf[-2:])
        return buf

    @staticmethod
    def parse_readings(raw):
        """Ultimeter stations emit data in PeetBros format.  Each line has 52
        characters - 2 header bytes, 48 data bytes, and a carriage return
        and line feed (new line):

        !!000000BE02EB000027700000023A023A0025005800000000\r\n
          SSSSXXDDTTTTLLLLPPPPttttHHHHhhhhddddmmmmRRRRWWWW

          SSSS - wind speed (0.1 kph)
          XX   - wind direction calibration
          DD   - wind direction (0-255)
          TTTT - outdoor temperature (0.1 F)
          LLLL - long term rain (0.01 in)
          PPPP - pressure (0.1 mbar)
          tttt - indoor temperature (0.1 F)
          HHHH - outdoor humidity (0.1 %)
          hhhh - indoor humidity (0.1 %)
          dddd - date (day of year)
          mmmm - time (minute of day)
          RRRR - daily rain (0.01 in)
          WWWW - one minute wind average (0.1 kph)

        "pressure" reported by the Ultimeter 2000 is correlated to the local
        official barometer reading as part of the setup of the station
        console so this value is assigned to the 'barometer' key and
        the pressure and altimeter values are calculated from it.

        Some stations may omit daily_rain or wind_average, so check for those.
        """
        buf = raw[2:]
        data = dict()
        data['windSpeed'] = Station._decode(buf[0:4], 0.1 * MILE_PER_KM)  # mph
        data['windDir'] = Station._decode(buf[6:8], 1.411764)  # compass deg
        data['outTemp'] = Station._decode(buf[8:12], 0.1, neg=True)  # degree_F
        data['rain_total'] = Station._decode(buf[12:16], 0.01)  # inch
        data['barometer'] = Station._decode(buf[16:20], 0.1 * INHG_PER_MBAR)  # inHg
        data['inTemp'] = Station._decode(buf[20:24], 0.1, neg=True)  # degree_F
        data['outHumidity'] = Station._decode(buf[24:28], 0.1)  # percent
        data['inHumidity'] = Station._decode(buf[28:32], 0.1)  # percent
        data['day_of_year'] = Station._decode(buf[32:36])
        data['minute_of_day'] = Station._decode(buf[36:40])
        if len(buf) > 40:
            data['daily_rain'] = Station._decode(buf[40:44], 0.01)  # inch
        if len(buf) > 44:
            data['wind_average'] = Station._decode(buf[44:48], 0.1 * MILE_PER_KM)  # mph
        return data

    @staticmethod
    def _decode(s, multiplier=None, neg=False):
        """Ultimeter puts hyphens in the string when a sensor is not installed.
        When we get a hyphen or any other non-hex character, return None.
        Negative values are represented in twos complement format.  Only do the
        check for negative values if requested, since some parameters use the
        full set of bits (e.g., wind direction) and some do not
        (e.g., temperature).
        """
        v = None
        try:
            v = int(s, 16)
            if neg:
                bits = 4 * len(s)
                if v & (1 << (bits - 1)) != 0:
                    v -= (1 << bits)
            if multiplier is not None:
                v *= multiplier
        except ValueError as e:
            if s != b'----':
                logdbg("decode failed for '%s': %s" % (s, e))
        return v


class UltimeterConfEditor(weewx.drivers.AbstractConfEditor):
    @property
    def default_stanza(self):
        return """
[Ultimeter]
    # This section is for the PeetBros Ultimeter series of weather stations.

    # Serial port such as /dev/ttyS0, /dev/ttyUSB0, or /dev/cua0
    port = %s

    # The station model, e.g., Ultimeter 2000, Ultimeter 100
    model = Ultimeter

    # The driver to use:
    driver = weewx.drivers.ultimeter
""" % Station.DEFAULT_PORT

    def prompt_for_settings(self):
        print("Specify the serial port on which the station is connected, for")
        print("example: /dev/ttyUSB0 or /dev/ttyS0 or /dev/cua0.")
        port = self._prompt('port', Station.DEFAULT_PORT)
        return {'port': port}


# define a main entry point for basic testing of the station without weewx
# engine and service overhead.  invoke this as follows from the weewx root dir:
#
# PYTHONPATH=bin python bin/weewx/drivers/ultimeter.py

if __name__ == '__main__':
    import optparse

    usage = """%prog [options] [--help]"""

    syslog.openlog('ultimeter', syslog.LOG_PID | syslog.LOG_CONS)
    syslog.setlogmask(syslog.LOG_UPTO(syslog.LOG_DEBUG))
    parser = optparse.OptionParser(usage=usage)
    parser.add_option('--version', dest='version', action='store_true',
                      help='display driver version')
    parser.add_option('--debug', dest='debug', action='store_true',
                      help='provide additional debug output in log')
    parser.add_option('--port', dest='port', metavar='PORT',
                      help='serial port to which the station is connected',
                      default=Station.DEFAULT_PORT)
    (options, args) = parser.parse_args()

    if options.version:
        print("ultimeter driver version %s" % DRIVER_VERSION)
        exit(0)

    with Station(options.port, debug_serial=options.debug) as station:
        station.set_logger_mode()
        while True:
            print(time.time(), _fmt(station.get_readings()))
