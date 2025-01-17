#!/usr/bin/env python2
import struct
import threading
from time import sleep
from typing import Optional

import datetime
import socket
import queue as queue2k
import random

from ppadb.device import Device
from ppadb.connection import Connection
from ppadb.client import Client as AdbClient

from . import hci
from .utils import bytes_to_hex
from .utils.packing import u32
from .core import InternalBlue


class ADBCore(InternalBlue):
    def __init__(
        self,
        queue_size=1000,
        btsnooplog_filename="btsnoop.log",
        log_level="info",
        serial=False,
        data_directory=".",
        replay=False,
    ):
        super(ADBCore, self).__init__(
            queue_size,
            btsnooplog_filename,
            log_level,
            data_directory,
            replay,
        )

        # Connect to adb device
        self.hciport: Optional[int] = None  # hciport is the port number of the forwarded HCI snoop port (8872). The inject port is at hciport+1
        self.serial = serial  # use serial su busybox scripting and do not try bluetooth.default.so
        self.doublecheck = False
        self.client = AdbClient(host="127.0.0.1", port=5037)

    def device(self) -> Device:
        return self.client.device(self.interface)

    def device_list(self):
        """
        Get a list of the connected devices
        """

        if self.exit_requested:
            self.shutdown()

        if self.running:
            self.logger.warning("Already running. call shutdown() first!")
            return []

        if self.replay:
            return [(self, "adb_replay", "adb: ReplayDevice")]
        # Check for connected adb devices
        try:
            adb_devices = self.client.devices()
        except ValueError:
            self.logger.info(
                "Could not find devices with pwnlib. If you see devices with `adb devices`, try to remove the lines 'for field in fields[2:]:... = v' in `pwnlib/adb/adb.py`."
            )
            adb_devices = 0
        except:
            adb_devices = 0

        if adb_devices == 0 or len(adb_devices) == 0:
            self.logger.info("No adb devices found.")
            return []

        # At least one device found
        self.logger.info("Found multiple adb devices")

        # Enumerate over found devices and put them into an array of tuple
        # First index is a self reference of the class
        # Second index is the identifier which is passed to connect()
        # Third index is the label which is shown in options(...)
        device_list = []
        for d in adb_devices:
            device_list.append((self, d.serial, "adb: %s (%s)" % (d.get_serial_no(), d.get_properties()['ro.product.model'])))

        return device_list

    def local_connect(self):
        """
        Start the framework by connecting to the Bluetooth Stack of the Android
        device via adb and the debugging TCP ports.
        """

        # setup sockets
        # on magisk-rooted devices there is sometimes already a read socket and this first setup needs to be skipped...
        if not self.serial:
            if not self._setupSockets():
                self.logger.info("Could not connect using Bluetooth module.")
                self.logger.info(
                    "Trying to set up connection for rooted smartphone with busybox installed."
                )
            else:
                return True  # successfully finished setup with bluetooth.default.so

        if not self._setupSerialSu():
            self.logger.critical("Failed to setup scripts for rooted devices.")
            return False

        # try again
        if not self._setupSockets():
            self.logger.critical("No connection to target device.")
            self.logger.info(
                "Check if:\n -> Bluetooth is active\n -> Bluetooth Stack has Debug Enabled\n -> BT HCI snoop log is activated\n -> USB debugging is authorized\n"
            )
            return False

        return True

    def _read_btsnoop_hdr(self):
        """
        Read the btsnoop header (see RFC 1761) from the snoop socket (s_snoop).
        """

        data = self.s_snoop.recv(16)
        if len(data) < 16:
            return None
        if (self.write_btsnooplog) and self.btsnooplog_file.tell() == 0:
            self.btsnooplog_file.write(data)
            self.btsnooplog_file.flush()

        btsnoop_hdr = (
            data[:8],
            u32(data[8:12], endian="big"),
            u32(data[12:16], endian="big"),
        )
        self.logger.debug("BT Snoop Header: %s, version: %d, data link type: %d" % btsnoop_hdr)
        return btsnoop_hdr

    def _btsnoop_parse_time(self, time):
        """
        Taken from: https://github.com/joekickass/python-btsnoop

        Record time is a 64-bit signed integer representing the time of packet arrival,
        in microseconds since midnight, January 1st, 0 AD nominal Gregorian.

        In order to avoid leap-day ambiguity in calculations, note that an equivalent
        epoch may be used of midnight, January 1st 2000 AD, which is represented in
        this field as 0x00E03AB44A676000.
        """
        time_betw_0_and_2000_ad = int("0x00E03AB44A676000", 16)
        time_since_2000_epoch = datetime.timedelta(
            microseconds=time
        ) - datetime.timedelta(microseconds=time_betw_0_and_2000_ad)
        return datetime.datetime(2000, 1, 1) + time_since_2000_epoch

    def _recvThreadFunc(self):
        """
        This is the run-function of the recvThread. It receives HCI events from the
        s_snoop socket. The HCI packets are encapsulated in btsnoop records (see RFC 1761).
        Received HCI packets are being put into the queues inside registeredHciRecvQueues and
        passed to the callback functions inside registeredHciCallbacks.
        The thread stops when exit_requested is set to True. It will do that on its own
        if it encounters a fatal error or the stackDumpReceiver reports that the chip crashed.
        """

        self.logger.debug("Receive Thread started.")

        while not self.exit_requested:
            # Read the record header
            record_hdr = b""
            while not self.exit_requested and len(record_hdr) < 24:
                try:
                    recv_data = self.s_snoop.recv(24 - len(record_hdr))
                    self.logger.debug(
                        "recvThreadFunc: received bt_snoop data "
                        + bytes_to_hex(recv_data)
                    )
                    if len(recv_data) == 0:
                        self.logger.info(
                            "recvThreadFunc: bt_snoop socket was closed by remote site. stopping recv thread..."
                        )
                        self.exit_requested = True
                        break
                    record_hdr += recv_data
                except socket.timeout:
                    pass  # this is ok. just try again without error

            if not record_hdr or len(record_hdr) != 24:
                if not self.exit_requested:
                    self.logger.warning("recvThreadFunc: Cannot recv record_hdr. stopping.")
                    self.exit_requested = True
                break

            if self.write_btsnooplog:
                self.btsnooplog_file.write(record_hdr)
                self.btsnooplog_file.flush()

            orig_len, inc_len, flags, drops, time64 = struct.unpack(
                ">IIIIq", record_hdr
            )

            # Read the record data
            record_data = bytearray()
            while not self.exit_requested and len(record_data) < inc_len:
                try:
                    recv_data = self.s_snoop.recv(inc_len - len(record_data))
                    if len(recv_data) == 0:
                        self.logger.info(
                            "recvThreadFunc: bt_snoop socket was closed by remote site. stopping.."
                        )
                        self.exit_requested = True
                        break
                    record_data += bytearray(recv_data)
                except socket.timeout:
                    pass  # this is ok. just try again without error

            if not record_data or len(record_data) != inc_len:
                if not self.exit_requested:
                    self.logger.warning("recvThreadFunc: Cannot recv data. stopping.")
                    self.exit_requested = True
                break

            if self.write_btsnooplog:
                self.btsnooplog_file.write(record_data)
                self.btsnooplog_file.flush()

            try:
                parsed_time = self._btsnoop_parse_time(time64)
            except OverflowError:
                parsed_time = None

            # Put all relevant infos into a tuple. The HCI packet is parsed with the help of hci.py.
            record = (
                hci.parse_hci_packet(record_data),
                orig_len,
                inc_len,
                flags,
                drops,
                parsed_time,
            )

            # self.logger.debug(
            #     "_recvThreadFunc Recv: [" + str(parsed_time) + "] " + str(record[0])
            # )

            # Put the record into all queues of registeredHciRecvQueues if their
            # filter function matches.
            for queue, filter_function in self.registeredHciRecvQueues:
                if filter_function == None or filter_function(record):
                    try:
                        queue.put(record, block=False)
                    except queue2k.Full:
                        self.logger.warning(
                            "recvThreadFunc: A recv queue is full. dropping packets.."
                        )

            # Call all callback functions inside registeredHciCallbacks and pass the
            # record as argument.
            for callback in self.registeredHciCallbacks:
                callback(record)

            # Check if the stackDumpReceiver has noticed that the chip crashed.
            # if self.stackDumpReceiver and self.stackDumpReceiver.stack_dump_has_happened:
            # A stack dump has happened!
            # self.logger.warning("recvThreadFunc: The controller sent a stack dump.")
            # self.exit_requested = True

        self.logger.debug("Receive Thread terminated.")

    def _setupSockets(self):
        """
        Forward the HCI snoop and inject ports from the Android device to
        the host (using adb). Open TCP sockets (s_snoop, s_inject) to connect
        to the forwarded ports. Read the btsnoop header from the s_snoop
        socket in order to verify that the connection actually works correctly.
        """

        # In order to support multiple parallel instances of InternalBlue
        # (with multiple attached Android devices) we must not hard code the
        # forwarded port numbers. Therefore we choose the port numbers
        # randomly and hope that they are not already in use.
        self.hciport = random.randint(
            60000, 65534
        )  # minus 1, as we are using hciport + 1
        self.logger.debug(
            "_setupSockets: Selected random ports snoop=%d and inject=%d"
            % (self.hciport, self.hciport + 1)
        )

        # Forward ports 8872 and 8873. Ignore self.logger.info() outputs by the adb function.
        self.device().forward(f"tcp:{self.hciport}", "tcp:8872")
        self.device().forward(f"tcp:{self.hciport+1}", "tcp:8873")

        # Connect to hci injection port
        self.s_inject = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.s_inject.connect(("127.0.0.1", self.hciport + 1))
            self.s_inject.settimeout(0.5)
        except socket.error:
            self.logger.warning("Could not connect to adb. Is your device authorized?")
            return False

        # Connect to hci snoop log port
        self.s_snoop = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s_snoop.connect(("127.0.0.1", self.hciport))
        self.s_snoop.settimeout(0.5)

        # Read btsnoop header
        if self._read_btsnoop_hdr() is None:
            self.logger.warning("Could not read btsnoop header")
            self.s_inject.close()
            self.s_snoop.close()
            self.s_inject = self.s_snoop = None
            self.device().killforward_all()
            return False
        return True

    def _teardownSockets(self):
        """
        Close s_snoop and s_inject sockets. Remove port forwarding with adb.
        """

        if self.s_inject != None:
            self.s_inject.close()
            self.s_inject = None
        if self.s_snoop != None:
            self.s_snoop.close()
            self.s_snoop = None

        if self.hciport is not None:
            hciport = self.hciport
            self.device().killforward_all()

    def _spawn(self, cmd: str):
        conn: Connection = self.device().create_connection()
        cmd = "exec:{}".format(cmd)
        conn.send(cmd)
        while True:
            sleep(1)

    def _setupSerialSu(self):
        """
        To run on any rooted device, we can also use some shellscripting.
        This is slower but at least works on any device.
        Commands on a S10e with Samsung Stock ROM + Magisk + busybox:

             tail -f -n +0 /data/log/bt/btsnoop_hci.log | nc -l -p 8872

             nc -l -p 8873 >/sdcard/internalblue_input.bin
             tail -f /sdcard/internalblue_input.bin >>/dev/ttySAC1

        Locations of the Bluetooth serial interface and btsnoop log file might differ.
        The second part *could* be combined, but it somehow does not work (SELinux?).

        The ADB Python bindings will kill the processes automatically :)

        """

        # In sending direction, the format is different.
        self.serial = True

        # check dependencies
        which_cmd = '''
        echo $PATH | while read -d: directory; do
            [ -x "$directory/{name}" ] || continue;
            echo -n "$directory/{name}\\x00";
        done
        [ -x "{name}" ] && echo -n "$PWD/{name}\\x00"
        '''.format(name="su")
        su_path = self.device().shell(f"sh -c '{which_cmd}'")
        if su_path is None or len(su_path) == 0:
            self.logger.critical("su not found, rooted smartphone required!")
            return False

        if self.device().shell("su -c 'which nc'") == "":
            self.logger.critical("nc not found, install busybox!")
            return False

        # automatically detect the proper serial device with lsof
        logfile = self.device().shell("su -c \"lsof | grep btsnoop_hci.log | tail -n 1\" | awk '{print $NF}'")[:-1]
        self.logger.info("Android btsnoop logfile %s...", logfile)
        interface = self.device().shell("su -c \"lsof | grep bluetooth | grep tty\" | awk '{print $NF}'")[:-1]
        self.logger.info("Android Bluetooth interface %s...", interface)

        if logfile == "":
            self.logger.critical("Could not find Bluetooth logfile. Enable Bluetooth snoop logging.")
            return False

        if interface == "":
            self.logger.critical("Could not find Bluetooth interface. Enable Bluetooth.")
            return False

        # spawn processes
        threading.Thread(target=self._spawn, args=(f"su -c \"tail -f -n +0 {logfile} | nc -l -p 8872\"",)).start()
        threading.Thread(target=self._spawn, args=(f"su -c \"nc -l -p 8873 >/sdcard/internalblue_input.bin\"",)).start()
        threading.Thread(target=self._spawn, args=(f"su -c \"tail -f /sdcard/internalblue_input.bin >>{interface}\"",)).start()
        sleep(2)

        return True
