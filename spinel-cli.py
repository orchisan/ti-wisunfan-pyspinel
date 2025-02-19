#!/usr/bin/env python3
#
#  Copyright (c) 2016-2019, The OpenThread Authors.
#  All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""
Shell tool for controlling Wi-SUN NCP instances.
"""

import os
import sys
import time
import traceback
import random
import importlib

import optparse

import binascii
import socket
import struct
import string
import textwrap

import logging
import logging.config
import logging.handlers

from cmd import Cmd

from spinel.const import SPINEL
from spinel.codec import WpanApi
from spinel.codec import SpinelCodec
from spinel.stream import StreamOpen
from spinel.tun import TunInterface
import spinel.config as CONFIG
import spinel.util as util

import ipaddress
import spinel_wisun_utils as wisun_util

__copyright__ = "Copyright (c) 2016 The OpenThread Authors. Modified by Texas Instruments for TI NCP Wi-SUN devices"
__version__ = "0.1.0"

MASTER_PROMPT = "spinel-cli"

import io
import spinel.ipv6 as ipv6
import spinel.common as common

DEFAULT_BAUDRATE = 115200
IPV6_ADDR_LEN    = 16
COAP_PORT        = 5683

COAP_RLED_ID = 0
COAP_GLED_ID = 1

class IPv6Factory(object):
    coap_port_factory = {COAP_PORT: ipv6.CoAPFactory()}
    ipv6_factory = ipv6.IPv6PacketFactory(
        ehf={
            0:
                ipv6.HopByHopFactory(
                    hop_by_hop_options_factory=ipv6.HopByHopOptionsFactory(
                        options_factories={109: ipv6.MPLOptionFactory(), 99: ipv6.RPLOptionFactory()})),
            43:
                ipv6.RoutingHeaderFactory(
                    routing_header_options_factory=ipv6.RoutingHeaderOptionsFactory(
                        options_factories={3: ipv6.SRHOptionFactory()}))
        },
        ulpf={
            17:
                ipv6.UDPDatagramFactory(
                    udp_header_factory=ipv6.UDPHeaderFactory(), dst_port_factories=coap_port_factory),
            58:
                ipv6.ICMPv6Factory(
                    body_factories={
                        128: ipv6.ICMPv6EchoBodyFactory(),
                        129: ipv6.ICMPv6EchoBodyFactory()
                    }
                )
        })

    def __init__(self):
        self.seq_number = 0
        self.mpl_seq_number = 0
        self.coap_msg_id = 0

    def _any_identifier(self):
        return random.getrandbits(16)

    def _get_next_seq_number(self):
        curr_seq = self.seq_number
        self.seq_number += 1
        return curr_seq

    def _get_next_mpl_seq_number(self):
        curr_mpl_seq = self.mpl_seq_number
        self.mpl_seq_number += 1
        return curr_mpl_seq

    def _get_next_coap_msg_id(self):
        curr_coap_msg_id = self.coap_msg_id
        self.coap_msg_id += 1
        return curr_coap_msg_id

    def build_icmp_echo_request(self,
                                src,
                                dst,
                                data,
                                hop_limit=64,
                                identifier=None,
                                sequence_number=None):
        identifier = self._any_identifier() if identifier is None else identifier
        sequence_number = self._get_next_seq_number() if sequence_number is None else sequence_number

        _extension_headers = None
        if ipaddress.IPv6Address(dst).is_multicast:
            # Tunnel the IPv6 header + frame (containing the multicast address)
            _extension_headers = [ipv6.HopByHop(options=[
                                 ipv6.HopByHopOption(ipv6.HopByHopOptionHeader(_type=0x6d),
                                 ipv6.MPLOption(S=3, M=0, V=0, sequence=self._get_next_mpl_seq_number(),
                                 seed_id=ipaddress.ip_address(src).packed))])]
            _extension_headers.append(ipv6.IPv6Header(source_address=src,
                                                      destination_address=dst,
                                                      hop_limit=hop_limit))
            dst = "ff03::fc" # Use the realm-all-forwarders address for the outer ipv6 header

        ping_req = ipv6.IPv6Packet(
            ipv6_header=ipv6.IPv6Header(source_address=src,
                                        destination_address=dst,
                                        hop_limit=hop_limit),
            upper_layer_protocol=ipv6.ICMPv6(
                header=ipv6.ICMPv6Header(_type=ipv6.ICMP_ECHO_REQUEST, code=0),
                body=ipv6.ICMPv6EchoBody(identifier=identifier,
                                         sequence_number=sequence_number,
                                         data=data)),
            extension_headers=_extension_headers)

        return ping_req.to_bytes()

    def build_icmp_echo_response(self,
                                src,
                                dst,
                                data,
                                hop_limit=64,
                                identifier=None,
                                sequence_number=0):
        identifier = self._any_identifier() if identifier is None else identifier

        ping_req = ipv6.IPv6Packet(
            ipv6_header=ipv6.IPv6Header(source_address=src,
                                        destination_address=dst,
                                        hop_limit=hop_limit
            ),
            upper_layer_protocol=ipv6.ICMPv6(
                header=ipv6.ICMPv6Header(_type=ipv6.ICMP_ECHO_RESPONSE, code=0),
                body=ipv6.ICMPv6EchoBody(identifier=identifier,
                                         sequence_number=sequence_number,
                                         data=data
                )
            )
        )

        return ping_req.to_bytes()

    def build_coap_request(self, src, dst, coap_type, coap_method_code, uri_path, option_list,
                           led_target=None, led_state=None, hop_limit=64):
        # Add and sort options
        coap_options = [ipv6.CoAPOption(ipv6.COAP_OPTION_URI_PATH, uri_path.encode('utf-8'))]
        if option_list is not None:
            coap_options += option_list
        coap_options.sort(key=lambda option: option.option_num)

        # Build payload if necessary
        coap_payload = None
        if led_target is not None and led_state is not None:
            coap_payload = ipv6.CoAPPayload(bytes([led_target, led_state]))

        coap_request = ipv6.IPv6Packet(
            ipv6_header=ipv6.IPv6Header(source_address=src,
                                        destination_address=dst,
                                        hop_limit=hop_limit
            ),
            upper_layer_protocol=ipv6.UDPDatagram(
                header=ipv6.UDPHeader(src_port=COAP_PORT, dst_port=COAP_PORT),
                payload=ipv6.CoAP(
                    header=ipv6.CoAPHeader(_type=coap_type, tkl=0, code=coap_method_code,
                        msg_id=self._get_next_coap_msg_id(), token=None, options = coap_options
                    ),
                    payload=coap_payload
                )
            )
        )

        return coap_request.to_bytes()

    def from_bytes(self, data):
        return self.ipv6_factory.parse(io.BytesIO(data), common.MessageInfo())

class SpinelCliCmd(Cmd, SpinelCodec):
    """
    A command line shell for controlling OpenThread NCP nodes
    via the Spinel protocol.
    """
    VIRTUAL_TIME = os.getenv('VIRTUAL_TIME') == '1'
    # key is IP Address, values are listed below
    routing_table_dict = dict()
    """dict of routing table entries
            routing_table_dict["<IPv6>"]["prefixLen"]  = prefixLength
            routing_table_dict["<IPv6>"]["nextHopAddr"]  = IPv6address
            routing_table_dict["<IPv6>"]["lifetime"] = lifetime
    """

    ipv6_factory = IPv6Factory()

    def _get_routing_table(self):
        return self.routing_table_dict

    def _init_virtual_time(self):
        """
        compute addresses used for virtual time.
        """
        BASE_PORT = 9000
        MAX_NODES = 34
        PORT_OFFSET = int(os.getenv("PORT_OFFSET", "0"))

        self._addr = ('127.0.0.1', BASE_PORT * 2 + MAX_NODES * PORT_OFFSET)
        self._simulator_addr = ('127.0.0.1',
                                BASE_PORT + MAX_NODES * PORT_OFFSET)


    # reset command, ifconfig, wisunstack start should clear the table
    # LAST_PROP_STATUS with a reason as RESET should also trigger clearing of routing table
    def clear_routing_table(self):
        self.routing_table_dict.clear()

    def __init__(self, stream, nodeid, vendor_module, *_a, **kw):
        if self.VIRTUAL_TIME:
            self._init_virtual_time()
        self.nodeid = nodeid
        self.tun_if = None

        self.wpan_api = WpanApi(stream, nodeid, vendor_module=vendor_module)
        self.wpan_api.queue_register(SPINEL.HEADER_DEFAULT)

        if kw.get('wpan_cb') is not None:
            self.wpan_callback = kw['wpan_cb']
            print("Changing callback function")

        self.wpan_api.callback_register(SPINEL.PROP_STREAM_NET,
                                        self.wpan_callback)

        self.wpan_api.callback_register(SPINEL.PROP_ROUTING_TABLE_UPDATE,
                                        self.wpan_routing_table_update_cb)

        Cmd.__init__(self)
        Cmd.identchars = string.ascii_letters + string.digits + '-'

        if sys.stdin.isatty():
            self.prompt = MASTER_PROMPT + " > "
        else:
            self.use_rawinput = 0
            self.prompt = ""

        SpinelCliCmd.command_names.sort()

        self.history_filename = os.path.expanduser("~/.spinel-cli-history")

        try:
            import readline
            try:
                readline.read_history_file(self.history_filename)
            except IOError:
                pass
        except ImportError:
            print("Module readline unavailable")
        else:
            import rlcompleter
            if 'libedit' in readline.__doc__:
                readline.parse_and_bind('bind ^I rl_complete')
            else:
                readline.parse_and_bind('tab: complete')

        # if hasattr(stream, 'pipe'):
        #     self.wpan_api.queue_wait_for_prop(SPINEL.PROP_LAST_STATUS,
        #                                      SPINEL.HEADER_ASYNC)

    command_names = [
        # Shell commands
        'exit',
        'quit',
        'clear',
        'history',
        #'debug',
        #'debug-mem',
        'v',
        'q',

        # Wi-SUN CLI commands
        'help',

        # properties in CORE category
        'protocolversion',
        'ncpversion',
        'interfacetype',
        'hwaddress',

        # properties in PHY category
        'ccathreshold',
        'txpower',

        # properties in MAC category
        'panid',

        # properties in NET category
        'ifconfig',
        'wisunstack',
        'role',
        'networkname',
        'ping',

        # properties in TI Wi-SUN specific PHY category
        'region',
        'phymodeid',
        'unicastchlist',
        'broadcastchlist',
        'asyncchlist',
        'chspacing',
        'ch0centerfreq',

        # properties in TI Wi-SUN specific MAC category
        'ucdwellinterval',
        'bcdwellinterval',
        'bcinterval',
        'ucchfunction',
        'bcchfunction',
        'macfilterlist',
        'macfiltermode',

        # properties in TI Wi-SUN specific NET category
        'routerstate',
        'dodagroute',
        'revokeDevice',

        # properties in IPV6 category
        'ipv6addresstable',
        'multicastlist',
        'coap',
        'numconnected',
        'connecteddevices',

        #reset cmd
        'reset',
        'nverase',
    ]

    @classmethod
    def update_routing_dict(self, value):
        print("Routing table update")
        try:
            changed_info, dst_ip_addr, routing_entry = wisun_util.parse_routingtable_property(value)
            print(changed_info)
            if changed_info == SPINEL.ROUTING_TABLE_ENTRY_DELETED:
                # remove from list
                if(self.routing_table_dict.get(dst_ip_addr) is not None):
                    # remove the specific routing table entry
                    self.routing_table_dict.pop(dst_ip_addr, None)
            elif changed_info == SPINEL.ROUTING_TABLE_ENTRY_CLEARED:
                # Clear entire routing table
                self.routing_table_dict.clear()
            else:
                #add/update entry
                self.routing_table_dict[dst_ip_addr] = routing_entry

        except RuntimeError:
            pass


    def wpan_callback(self, prop, value, tid):
        consumed = False
        if prop == SPINEL.PROP_STREAM_NET:
            consumed = True

            try:
                pkt = self.ipv6_factory.from_bytes(value)

                if CONFIG.DEBUG_LOG_PKT:
                    CONFIG.LOGGER.debug(pkt)

                if pkt.upper_layer_protocol.type == ipv6.IPV6_NEXT_HEADER_ICMP:
                    if pkt.upper_layer_protocol.header.type == ipv6.ICMP_ECHO_REQUEST:
                        print("\nEcho request: %d bytes from %s to %s, icmp_seq=%d hlim=%d. Sending echo response." %
                              (len(pkt.upper_layer_protocol.body.data),
                               pkt.ipv6_header.source_address,
                               pkt.ipv6_header.destination_address,
                               pkt.upper_layer_protocol.body.sequence_number,
                               pkt.ipv6_header.hop_limit))

                        # Generate echo response
                        ping_resp = self.ipv6_factory.build_icmp_echo_response(
                            src=pkt.ipv6_header.destination_address,
                            dst=pkt.ipv6_header.source_address,
                            data=pkt.upper_layer_protocol.body.data,
                            identifier=pkt.upper_layer_protocol.body.identifier,
                            sequence_number=pkt.upper_layer_protocol.body.sequence_number)

                        self.wpan_api.ip_send(ping_resp)
                        # Let handler print result
                    elif pkt.upper_layer_protocol.header.type == ipv6.ICMP_ECHO_RESPONSE:
                        timenow = int(round(time.time() * 1000)) & 0xFFFFFFFF
                        timestamp = (pkt.upper_layer_protocol.body.identifier << 16 |
                                     pkt.upper_layer_protocol.body.sequence_number)
                        timedelta = (timenow - timestamp)
                        print("\n%d bytes from %s: icmp_seq=%d hlim=%d time=%dms" %
                              (len(pkt.upper_layer_protocol.body.data),
                               pkt.ipv6_header.source_address,
                               pkt.upper_layer_protocol.body.sequence_number,
                               pkt.ipv6_header.hop_limit, timedelta))
                    else:
                        print("ICMP packet received")
                elif pkt.upper_layer_protocol.type == ipv6.IPV6_NEXT_HEADER_UDP:
                    udp_pkt = pkt.upper_layer_protocol
                    if udp_pkt.header.dst_port == COAP_PORT:
                        coap_pkt = pkt.upper_layer_protocol.payload
                        h = coap_pkt.header
                        p = coap_pkt.payload
                        print("\nCoAP packet received from {}: type: {} ({}), token len: {}, code: {}.{:02d} ({}), msg_id: {}".format(
                                 pkt.ipv6_header.source_address, h.type, ipv6.COAP_TYPE_NAME_LOOKUP[h.type],
                                 h.tkl, h.code[0], h.code[1], ipv6.COAP_CODE_NAME_LOOKUP[h.code], h.msg_id))
                        option_str = "CoAP options:"
                        for option in h.options:
                            option_str += " {} ({}): {},".format(ipv6.COAP_OPTION_NAME_LOOKUP[option.option_num],
                                                                 option.option_num, option.option_val)
                        option_str = option_str[:-1] # Strip last comma
                        print(option_str)
                        if len(p.payload) == 2:
                            rled_state = "Off" if int(p.payload[0]) == 0 else "On"
                            gled_state = "Off" if int(p.payload[1]) == 0 else "On"
                            print("RLED state: {}, GLED state: {}".format(rled_state, gled_state))
                        elif len(p.payload) == 0:
                            print("No CoAP payload")
                        else:
                            print("Raw CoAP payload: {}".format(p.payload))
                    else:
                        print("UDP packet received")
                else:
                    print("\nReceived IPv6 packet with unsupported upper layer protocol (not UDP or ICMP)")
            except RuntimeError:
                print("\n Incoming IPv6 Packet Decode Error")
                print(traceback.format_exc())
                pass
        return consumed

    @classmethod
    def wpan_routing_table_update_cb(cls, prop, value, tid):
        consumed = False
        if prop == SPINEL.PROP_ROUTING_TABLE_UPDATE:
            consumed = True
            cls.update_routing_dict(value)

        return consumed

    @classmethod
    def log(cls, text):
        """ Common log handler. """
        CONFIG.LOGGER.info(text)

    def parseline(self, line):
        cmd, arg, line = Cmd.parseline(self, line)
        if cmd:
            cmd = self.short_command_name(cmd)
            line = cmd + ' ' + arg
        return cmd, arg, line

    def completenames(self, text, *ignored):
        return [
            name + ' '
            for name in SpinelCliCmd.command_names
            if name.startswith(text) or
            self.short_command_name(name).startswith(text)
        ]

    @classmethod
    def short_command_name(cls, cmd):
        return cmd.replace('-', '')

    def postloop(self):
        try:
            import readline
            try:
                readline.write_history_file(self.history_filename)
            except IOError:
                pass
        except ImportError:
            pass

    def prop_get_value(self, prop_id):
        """ Blocking helper to return value for given propery identifier. """
        return self.wpan_api.prop_get_value(prop_id)

    def prop_set_value(self, prop_id, value, py_format='B'):
        """ Blocking helper to set value for given propery identifier. """
        return self.wpan_api.prop_set_value(prop_id, value, py_format)

    def prop_insert_value(self, prop_id, value, py_format='B'):
        """ Blocking helper to insert entry for given list property. """
        return self.wpan_api.prop_insert_value(prop_id, value, py_format)

    def prop_remove_value(self, prop_id, value, py_format='B'):
        """ Blocking helper to remove entry for given list property. """
        return self.wpan_api.prop_remove_value(prop_id, value, py_format)

    def prop_get_or_set_value(self, prop_id, line, mixed_format='B'):
        """ Helper to get or set a property value based on line arguments. """
        if line:
            value = self.prep_line(line, mixed_format)
            py_format = self.prep_format(value, mixed_format)
            value = self.prop_set_value(prop_id, value, py_format)
        else:
            value = self.prop_get_value(prop_id)

        return value

    @classmethod
    def prep_line(cls, line, mixed_format='B'):
        """ Convert a command line argument to proper binary encoding (pre-pack). """
        value = line
        if line != None:
            if mixed_format == 'U':  # For UTF8, just a pass through line unmodified
                line += '\0'
                value = line.encode('utf-8')
            elif mixed_format in (
                    'D',
                    'E'):  # Expect raw data to be hex string w/o delimeters
                value = util.hex_to_bytes(line)
            elif isinstance(line, str):
                # Most everything else is some type of integer
                value = int(line, 0)
        return value

    @classmethod
    def prep_format(cls, value, mixed_format='B'):
        """ Convert a spinel format to a python pack format. """
        py_format = mixed_format
        if value == "":
            py_format = '0s'
        elif mixed_format in ('D', 'U', 'E'):
            py_format = str(len(value)) + 's'
        return py_format

    def prop_get(self, prop_id, mixed_format='B'):
        """ Helper to get a propery and output the value with Done or Error. """
        value = self.prop_get_value(prop_id)
        if value is None:
            print("Error")
            return None

        if (mixed_format == 'D') or (mixed_format == 'E'):
            print(util.hexify_str(value, ''))
        else:
            print(str(value))
        print("Done")

        return value

    def prop_set(self, prop_id, line, mixed_format='B', output=True):
        """ Helper to set a propery and output Done or Error. """
        value = self.prep_line(line, mixed_format)
        py_format = self.prep_format(value, mixed_format)
        result = self.prop_set_value(prop_id, value, py_format)

        if not output:
            return result

        if result is None:
            print("Error")
        else:
            print("Done")

        return result

    def handle_property(self, line, prop_id, mixed_format='B', output=True):
        """ Helper to set property when line argument passed, get otherwise. """
        value = self.prop_get_or_set_value(prop_id, line, mixed_format)
        if not output:
            return value

        if value is None or value == "":
            print("Error")
            return None

        if line is None or line == "":
            # Only print value on PROP_VALUE_GET
            if mixed_format == '6':
                print(str(ipaddress.IPv6Address(value)))
            elif (mixed_format == 'D') or (mixed_format == 'E'):
                print(binascii.hexlify(value).decode('utf8'))
            elif mixed_format == 'H':
                if prop_id == SPINEL.PROP_MAC_15_4_PANID:
                    print("0x%04x" % value)
                else:
                    print("%04x" % value)
            elif mixed_format == 'B' or mixed_format == 'L':
                print(str(int.from_bytes(value, "little", signed=False)))
            else:
                print(str(value))

        print("Done")
        return value

    def do_help(self, line):
        if line:
            cmd, _arg, _unused = self.parseline(line)
            try:
                doc = getattr(self, 'do_' + cmd).__doc__
            except AttributeError:
                doc = None
            if doc:
                self.log("%s\n" % textwrap.dedent(doc))
            else:
                self.log("No help on %s\n" % (line))
        else:
            self.print_topics(
                "\nAvailable commands (type help <name> for more information):",
                SpinelCliCmd.command_names, 15, 80)

    def do_v(self, _line):
        """
        version
            Shows detailed version information on spinel-cli tool:
        """
        self.log(MASTER_PROMPT + " ver. " + __version__)
        self.log(__copyright__)

    @classmethod
    def do_clear(cls, _line):
        """ Clean up the display. """
        os.system('reset')

    def do_history(self, _line):
        """
        history
          Show previously executed commands.
        """

        try:
            import readline
            hist = readline.get_current_history_length()
            for idx in range(1, hist + 1):
                self.log(readline.get_history_item(idx))
        except ImportError:
            pass

    def do_h(self, line):
        """ Shortcut for history. """
        self.do_history(line)

    def do_exit(self, _line):
        """ Exit the shell. """
        self.log("exit")
        return True

    def do_quit(self, line):
        """ Exit the shell. """
        return self.do_exit(line)

    def do_q(self, line):
        """ Exit the shell. """
        return self.do_exit(line)

    def do_EOF(self, _line):
        """ End of file handler for when commands are piped into shell. """
        self.log("\n")
        return True

    def emptyline(self):
        pass

    def default(self, line):
        if line[0] == "#":
            CONFIG.LOGGER.debug(line)
        else:
            CONFIG.LOGGER.info(line + ": command not found")
            # exec(line)

    def do_debug(self, line):
        """
        Enables detail logging of bytes over the wire to the radio modem.
        Usage: debug <1=enable | 0=disable>
        """

        if line != None and line != "":
            level = int(line)
        else:
            level = 0

        CONFIG.debug_set_level(level)

    def do_debugmem(self, _line):
        """ Profile python memory usage. """
        from guppy import hpy
        heap_stats = hpy()
        print(heap_stats.heap())
        print()
        print(heap_stats.heap().byrcs)


	# Wi-SUN CLI commands

    # for Core properties
    def do_protocolversion(self, line):
        """
        protocol version

            Print the protocol version information: Major and Minor version number.

            > protocolversion
            1.0
            Done
        """
        #self.handle_property(line, SPINEL.PROP_PROTOCOL_VERSION, 'ii')
        self.handle_property(line, SPINEL.PROP_PROTOCOL_VERSION, 'U')

    def do_ncpversion(self, line):
        """
        ncp version

            Print the build version information.

            > ncpversion
            TIWISUNFAN/1.0; DEBUG; Feb 7 2021 18:22:04
            Done
        """
        self.handle_property(line, SPINEL.PROP_NCP_VERSION, 'U')


    def do_interfacetype(self, line):
        """
        Interface type

            Identifies the network protocol for the NCP . Will always return 4 (Wi-SUN FAN)

            > interfacetype
            4
            Done
        """
        self.handle_property(line, SPINEL.PROP_INTERFACE_TYPE, 'i')


    def do_hwaddress(self, line):
        """
        hwaddress

            Get the IEEE 802.15.4 Extended Address.

            > hwaddress
            dead00beef00cafe
            Done

        """
        self.handle_property(line, SPINEL.PROP_HWADDR, 'E')


    # for PHY properties
    def do_ccathreshold(self, line):
        """
        ccathreshold

            Get the CCA ED Threshold in dBm.

            > ccathreshold
            -10
            Done

        ccathreshold <ccathreshold>

            Set the CCA ED Threshold in dBm.

            > ccathreshold -70
            Done
        """
        self.handle_property(line, SPINEL.PROP_PHY_CCA_THRESHOLD, mixed_format='b')


    def do_txpower(self, line):
        """
        txpower

            Get the transmit power in dBm.

            > txpower
            0
            Done

        txpower <txpower>

            Set the transmit power in dBm.

            > txpower -10
            Done
        """
        self.handle_property(line, SPINEL.PROP_PHY_TX_POWER, mixed_format='b')

    # for MAC properties
    def do_panid(self, line):
        """
        panid

            Get the IEEE 802.15.4 PAN ID value. Applicable on Border Router side only.

            > panid
            0xdead
            Done

        panid <panid>

            Set the IEEE 802.15.4 PAN ID value.

            > panid 0xdead
            Done
        """
        self.handle_property(line, SPINEL.PROP_MAC_15_4_PANID, 'H')

    # for NET properties
    def complete_ifconfig(self, text, _line, _begidx, _endidx):
        """ Subcommand completion handler for ifconfig command. """
        map_sub_commands = ('up', 'down')
        return [i for i in map_sub_commands if i.startswith(text)]

    def do_ifconfig(self, line):
        """
        ifconfig up

            Bring up the Wi-SUN Network interface.

            > ifconfig up
            Done

        ifconfig down

            Bring down the Wi-SUN Network interface.

            > ifconfig down
            Done

        ifconfig

            Show the status of the Wi-SUN Network interface.

            > ifconfig
            down
            Done
        """

        self.clear_routing_table()
        params = line.split(" ")

        if params[0] == "":
            value = self.prop_get_value(SPINEL.PROP_NET_IF_UP)
            if value != None:
                map_arg_value = {
                    0: "down",
                    1: "up",
                }
                print(map_arg_value[value])

        elif params[0] == "up":
            self.prop_set(SPINEL.PROP_NET_IF_UP, '1')
            return

        elif params[0] == "down":
            self.prop_set(SPINEL.PROP_NET_IF_UP, '0')
            return

        print("Done")


    def complete_wisunstack(self, text, _line, _begidx, _endidx):
        """ Subcommand completion handler for thread command. """
        map_sub_commands = ('start', 'stop')
        return [i for i in map_sub_commands if i.startswith(text)]

    def do_wisunstack(self, line):
        """
        wisunstack start

            Enable Wi-SUN stack operation and attach to a Wi-SUN network.

            > wisunstack start
            Done

        wisunstack stop

            Disable Wi-SUN stack operation and detach from a Wi-SUN network.

            > wisunstack stop
            Done

        wisunstack

            Show the operational status of the Wi-SUN stack.

            > wisunstack
            stop
            Done
        """
        map_arg_value = {
            0: "stop",
            1: "start",
        }

        map_arg_name = {
            "stop": "0",
            "start": "1",
        }

        if line:
            try:
                # remap string state names to integer
                line = map_arg_name[line]

                if "1" in line:
                    # clear routing table if wisunstack start is called
                    self.clear_routing_table()
            except:
                print("Error")
                return

        result = self.prop_get_or_set_value(SPINEL.PROP_NET_STACK_UP, line)
        if result != None:
            if not line:
                print(map_arg_value[result])
            print("Done")
        else:
            print("Error")


    def do_role(self, line):
        """
        role

            Display the role of the device in the Wi-SUN network - Router, Border Router.

            > role
            1 : Router
            Done
        """

        value = self.prop_get_value(SPINEL.PROP_NET_ROLE)
        if value != None:
            map_arg_value = {
                0: "Border-Router",
                1: "Router",
            }
            print(str(value) + " : " + map_arg_value[value])

        print("Done")

    def do_networkname(self, line):
        """
        networkname

            Get the Wi-SUN Network Name.

            > networkname
            wisunnet
            Done

        networkname <name>

            Set the Wi-SUN Network Name. Max string length = 32 characters.

            > networkname wisunnet
            Done
        """

        self.handle_property(line, SPINEL.PROP_NET_NETWORK_NAME, 'U')


    # for TI Wi-SUN specific PHY properties

    def do_region(self, line):
        """
        region
            Get the Wi-SUN Network's regulatory region of operation.
            1 - NA, 2 - JP, 3 - EU, 7 - BZ, FF --> Custom region

            > region
            1
            Done

        """
        value = self.prop_get_value(SPINEL.PROP_PHY_REGION)
        if value != None:
            map_arg_value = {
                1: "North-America",
                2: "Japan",
                3: "Europe",
                7: "Brazil",
                255: "Custom",
            }
            print(str(value) + " : " + map_arg_value[value])

        print("Done")

    def do_phymodeid(self, line):
        """
        phymodeid
            Get the modeID set for Wi-SUN network's operation.
            Supported values (1-7)

            > phymodeid
            2
            Done

        """
        value = self.prop_get_value(SPINEL.PROP_PHY_MODE_ID)
        print(value)
        print("Done")

    def do_unicastchlist(self, line):
        """
        unicastchlist
            Get or Set the Bit Mask to specify what channels can be used for unicast transmissions.
            Each bit in the bit mask represents if the channel is present or not
            NA region has 129 channels maximum, thus max bit mask is 17 bytes long

            > unicastchlist
            Channel List = 0-128
            Bit Mask = ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:01

            > unicastchlist 0-128
            Channel List = 0-128
            Bit Mask = ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:01
            Done

            > unicastchlist 0-7:15-20:33-46
            Channel List = 0-7:15-20:33-46
            Bit Mask = ff:80:1f:00:fe:7f:00:00:00:00:00:00:00:00:00:00:00
            Done

            > unicastchlist
            Channel List = 0-7:15-20:33-46
            Bit Mask = ff:80:1f:00:fe:7f:00:00:00:00:00:00:00:00:00:00:00

        """
        params = line.split(" ")

        if params[0] == "": # get
            value = self.prop_get_value(SPINEL.PROP_PHY_UNICAST_CHANNEL_LIST)
            arr_value = [0]*17;
            for i in range(17):
                arr_value[i] = hex(int.from_bytes(value[i : (i+1)], "little", signed=False))
            byte_array_input_string = wisun_util.change_format_input_string(arr_value)
            chan_num_list = wisun_util.convert_to_chan_num_list(byte_array_input_string)
            print("Channel List = " + str(chan_num_list))
            print("Bit Mask = " + byte_array_input_string)
        else:
            converted_bitmask, inp_bytes = wisun_util.convert_to_bitmask(params[0])
            print("Channel List = " + str(params[0]))
            print("Bit Mask = " + wisun_util.format_display_string(str(converted_bitmask)))
            self.wpan_api.chlist_send(inp_bytes, SPINEL.PROP_PHY_UNICAST_CHANNEL_LIST)
        print("Done")

    def do_broadcastchlist(self, line):
        """
        broadcastchlist
            Get or Set the Bit Mask to specify what channels can be used for broadcast transmissions.
            Applicable only on the border router side.
            Bit Mask where each bit represents if the channel is present or not
            NA region has 129 channels maximum, thus max bit mask is 17 bytes long

            > broadcastchlist
            Channel List = 0-128
            Bit Mask = ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:01

            > broadcastchlist 0-128
            Channel List = 0-128
            Bit Mask = ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:01
            Done

            > broadcastchlist 0-7:15-20:33-46
            Channel List = 0-7:15-20:33-46
            Bit Mask = ff:80:1f:00:fe:7f:00:00:00:00:00:00:00:00:00:00:00
            Done

            > broadcastchlist
            Channel List = 0-7:15-20:33-46
            Bit Mask = ff:80:1f:00:fe:7f:00:00:00:00:00:00:00:00:00:00:00

        """
        params = line.split(" ")

        if params[0] == "": # get
            value = self.prop_get_value(SPINEL.PROP_PHY_BROADCAST_CHANNEL_LIST)
            arr_value = [0]*17;
            for i in range(17):
                arr_value[i] = hex(int.from_bytes(value[i : (i+1)], "little", signed=False))
            byte_array_input_string = wisun_util.change_format_input_string(arr_value)
            chan_num_list = wisun_util.convert_to_chan_num_list(byte_array_input_string)
            print("Channel List = " + str(chan_num_list))
            print("Bit Mask = " + byte_array_input_string)
        else:
            converted_bitmask, inp_bytes = wisun_util.convert_to_bitmask(params[0])
            print("Channel List = " + str(params[0]))
            print("Bit Mask = " + wisun_util.format_display_string(str(converted_bitmask)))
            self.wpan_api.chlist_send(inp_bytes, SPINEL.PROP_PHY_BROADCAST_CHANNEL_LIST)
        print("Done")

    def do_asyncchlist(self, line):
        """
        asyncchlist
            Get or Set the Bit Mask to specify what channels can be used for async transmissions.
            Bit Mask where each bit represents if the channel is present or not
            NA region has 129 channels maximum, thus max bit mask is 17 bytes long

            > asyncchlist
            Channel List = 0-128
            Bit Mask = ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:01

            > asyncchlist 0-128
            Channel List = 0-128
            Bit Mask = ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:ff:01
            Done

            > asyncchlist 0-7:15-20:33-46
            Channel List = 0-7:15-20:33-46
            Bit Mask = ff:80:1f:00:fe:7f:00:00:00:00:00:00:00:00:00:00:00
            Done

            > asyncchlist
            Channel List = 0-7:15-20:33-46
            Bit Mask = ff:80:1f:00:fe:7f:00:00:00:00:00:00:00:00:00:00:00

        """
        params = line.split(" ")
        if params[0] == "": # get
            value = self.prop_get_value(SPINEL.PROP_PHY_ASYNC_CHANNEL_LIST)

            arr_value = [0]*17;
            for i in range(17):
                arr_value[i] = hex(int.from_bytes(value[i : (i+1)], "little", signed=False))

            byte_array_input_string = wisun_util.change_format_input_string(arr_value)
            chan_num_list = wisun_util.convert_to_chan_num_list(byte_array_input_string)
            print("Channel List = " + str(chan_num_list))
            print("Bit Mask = " + byte_array_input_string)
        else:
            converted_bitmask, inp_bytes = wisun_util.convert_to_bitmask(params[0])
            print("Channel List = " + str(params[0]))
            print("Bit Mask = " + wisun_util.format_display_string(str(converted_bitmask)))
            self.wpan_api.chlist_send(inp_bytes, SPINEL.PROP_PHY_ASYNC_CHANNEL_LIST)
        print("Done")

    def do_chspacing(self, line):

        """
        chspacing
            Get the channel spacing in kHz.

            > chspacing
            100 kHz
            Done

        """
        value = self.prop_get_value(SPINEL.PROP_PHY_CH_SPACING)
        ans = int.from_bytes(value, "little", signed=False)
        print(str(ans) + " kHz")
        print("Done")

    def do_ch0centerfreq(self, line):

        """
        ch0centerfreq
            Get the Channel 0 Center frequency formatted as {Ch0-MHz, Ch0-KHz}.

            > ch0centerfreq
            {902,200}
            Done

        """
        value = self.prop_get_value(SPINEL.PROP_PHY_CHO_CENTER_FREQ)
        freqMHz = int.from_bytes(value[:2], "little", signed=False)
        freqkHz = int.from_bytes(value[2:4], "little", signed=False)
        print("{" + str(freqMHz) + " MHz, " + str(freqkHz) + " kHz}")
        print("Done")


    # for TI Wi-SUN specific MAC properties
    def do_ucdwellinterval(self, line):
        """
        ucdwellinterval
            Get or Set Unicast dwell Interval (0 - 255 ms)

            > ucdwellinterval
            100
            Done

            > ucdwellinterval  100
            Done

        """
        self.handle_property(line, SPINEL.PROP_MAC_UC_DWELL_INTERVAL, mixed_format='B')

    def do_bcdwellinterval(self, line):
        """
        bcdwellinterval
            Get or Set Broadcast dwell Interval (0 - 255 ms).
            Applicable only on the border router side.

            > bcdwellinterval
            100
            Done

            > bcdwellinterval  100
            Done

        """
        self.handle_property(line, SPINEL.PROP_MAC_BC_DWELL_INTERVAL, mixed_format='B')


    def do_bcinterval(self, line):
        """
        bcinterval
            Get or Set Broadcast Interval (0 - 0xFFFFFF ms).
            Applicable only on the border router side.

            > bcinterval
            0xFFFF
            Done

            > bcinterval  0xFFFF
            Done

        """
        self.handle_property(line, SPINEL.PROP_MAC_BC_INTERVAL, mixed_format = 'L')


    def do_ucchfunction(self, line):
        """
        ucchfunction
            Get or Set Unicast Channel Function.
            0 - Fixed, 2 - Hopping based on DH1CF

            > ucchfunction
            1
            Done

            > ucchfunction  2
            Done

        """
        self.handle_property(line, SPINEL.PROP_MAC_UC_CHANNEL_FUNCTION, mixed_format = 'B')

    def do_bcchfunction(self, line):
        """
        bcchfunction
            Get or Set Broadcast Channel Function.
            0 - Fixed, 1 - Hopping based on DH1CF
            Applicable only on the border router side.

            > bcchfunction
            1
            Done

            > bcchfunction  1
            Done

        """
        self.handle_property(line, SPINEL.PROP_MAC_BC_CHANNEL_FUNCTION, mixed_format = 'B')


    def do_macfiltermode(self, line):
        """
        macfiltermode
            Get or Set the filtering mode at MAC layer.
            0 - Disabled, 1 - Whitelist, 2 - Blacklist

            > macfiltermode
            1
            Done

            > macfiltermode  1
            Done

        """
        self.handle_property(line, SPINEL.PROP_MAC_FILTER_MODE, mixed_format='b')

    # for TI Wi-SUN specific NET properties
    def do_revokeDevice(self, line):
        """
        revokeDevice

            Write-only property intended to remove rogue devices from network.

            > revokeDevice dead00beef00cafe
            Done

        """
        if line == None or line == '':
            print("\n Error: Please specify EUI-64 to whom access needs to be revoked")
            return

        self.handle_property(line, SPINEL.PROP_REVOKE_GTK_HWADDR, 'E')

    def do_ping(self, line):
        """
        ping <ipaddr> [size] [count] [interval]

            Send an ICMPv6 Echo Request.

            > ping fdde:ad00:beef:0:558:f56b:d688:799
            16 bytes from fdde:ad00:beef:0:558:f56b:d688:799: icmp_seq=1 hlim=64 time=28ms
        """
        params = line.split(" ")
        addr = "::1"
        _size = "56"
        _count = "1"
        _interval = "1"
        if len(params) > 0:
            addr = params[0]
        if len(params) > 1:
            _size = params[1]
        if len(params) > 2:
            _count = params[2]
        if len(params) > 3:
            _interval = params[3]

        try:
            # Generate local ping packet and send directly via spinel.
            value = self.prop_get_value(SPINEL.PROP_IPV6_ADDRESS_TABLE)
            ipv6AddrTableList = self._parse_ipv6addresstable_property(value)
            srcIPAddress = "None"
            for i in range(0,len(ipv6AddrTableList)):
                if('0xFE80' not in str(ipv6AddrTableList[i]["ipv6Addr"])):
                    srcIPAddress = str(ipv6AddrTableList[i]["ipv6Addr"])
                    break

            if srcIPAddress == "None":
                print("Cannot Perform Ping as device does not have a valid Source IP Address")
                return

            timenow = int(round(time.time() * 1000)) & 0xFFFFFFFF
            data = bytearray(int(_size))

            ping_req = self.ipv6_factory.build_icmp_echo_request(
                srcIPAddress,
                addr,
                data,
                identifier=(timenow >> 16),
                sequence_number=(timenow & 0xffff))

            self.wpan_api.ip_send(ping_req)
            # Let handler print result
        except:
            print("Fail")
            print(traceback.format_exc())

    def do_macfilterlist(self, line):
        """
        macfilterList

           Display the addressfilter based on the value set using macfiltermode

        macfilterlist add <extaddr>

            Add an IEEE 802.15.4 Extended Address to the address filter.

            > macfilterlist add dead00beef00cafe
            Done

        macfilterlist remove <extaddr>

            Remove an IEEE 802.15.4 Extended Address from the address filter.

            > macfilter remove dead00beef00caff
            Done
        """
        params = line.split(" ")
        if params[0] == "":
            value = self.prop_get_value(SPINEL.PROP_MAC_FILTER_MODE)
            if value == 0 or value is None:
                print("Error: set the filter mode first: 1 for accessing WhiteList and 2 for accessing BlackList")
                return value

            #get and display the content of BlackList/WhiteList
            value = self.prop_get_value(SPINEL.PROP_MAC_MAC_FILTER_LIST)

            size = 0x8
            # break the byte stream into different entries
            addrEntries = [value[i:i + size] for i in range(0, len(value), size)]
            #print each address entry
            for addrEntry in addrEntries:
                print(binascii.hexlify(addrEntry).decode('utf8'))

        elif params[0] == "add":
            arr = util.hex_to_bytes(params[1])
            self.prop_insert_value(SPINEL.PROP_MAC_MAC_FILTER_LIST, arr, str(len(arr)) + 's')

        elif params[0] == "remove":
            arr = util.hex_to_bytes(params[1])
            self.prop_remove_value(SPINEL.PROP_MAC_MAC_FILTER_LIST, arr, str(len(arr)) + 's')

    # for TI Wi-SUN specific NET properties
    def do_routerstate(self, line):
        """
        routerstate
            Display the current join state of the Wi-SUN router device. The different states are:
            0: "Idle",
            1: "Scanning for suitable network",
            2: "Authentication in Progress",
            3: "Acquiring PAN Configuration",
            4: "Configuring Routing & DHCP based Unique IPv6 address",
            5: "Successfully joined and operational"

            > routerstate
            5
            Successfully joined and operational
            Done
        """
        map_arg_value = {
            0: "Idle",
            1: "Scanning for suitable network",
            2: "Authentication in Progress",
            3: "Acquiring PAN Configuration",
            4: "Configuring Routing & DHCP based Unique IPv6 address",
            5: "Successfully joined and operational"
        }

        result = self.prop_get_value(SPINEL.PROP_NET_STATE)
        print(result)
        if result != None:
            state = map_arg_value[result]
            print(state)
            print("Done")
        else:
            print("Error")

    def do_multicastlist(self, line):
        """
        multicastlist

           Display the multicast groups this device is subscribed to. Note that this command only displays
           multicast groups above realm scope (scop 3). The device is already subscribed to existing well-known
           interface, link, and realm-local multicast groups as specified by the Wi-SUN standard.

        multicastlist add <ipv6addr>

            Add an IPv6 address to the MPL domain and multicast group list for this device. Note that this
            command can only add groups above realm scope (scop 3).

            > multicastlist add ff05::3
            Done

        multicastlist remove <ipv6addr>

            Remove an IPv6 from the MPL domain and multicast group list for this device. Note that this
            command can only remove groups above realm scope (scop 3).

            > multicastlist remove ff05::3
            Done
        """
        router_state = self.prop_get_value(SPINEL.PROP_NET_STATE)
        if router_state < 5:
            print("Error: Device must be in join state 5 (Successfully joined and operational) to process multicast commands")
            return

        params = line.split(" ")
        if params[0] == "":
            value = self.prop_get_value(SPINEL.PROP_MULTICAST_LIST)
            # Break the byte stream into different entries (skipping the first 2 length bytes)
            addrEntries = [value[i:i + IPV6_ADDR_LEN] for i in range(2, len(value), IPV6_ADDR_LEN)]
            for addrEntry in addrEntries:
                print(ipaddress.IPv6Address(addrEntry))

        elif params[0] == "add" or params[0] == "remove":
            try:
                ipaddr = ipaddress.IPv6Address(params[1])
            except:
                print("Error: Invalid IPv6 address")
                return
            if not ipaddr.is_multicast:
                print("Error: IPv6 address is not multicast")
                return

            if params[0] == "add":
                self.prop_insert_value(SPINEL.PROP_MULTICAST_LIST, ipaddr.packed, str(len(ipaddr.packed)) + 's')
            elif params[0] == "remove":
                self.prop_remove_value(SPINEL.PROP_MULTICAST_LIST, ipaddr.packed, str(len(ipaddr.packed)) + 's')

    def do_coap(self, line):
        """
        coap <ipv6 address> <coap request code (get|put|post)> <coap request type (con|non)> <uri_path>
             [--led_state <led_target (r|g)> <led_state (0|1)>]
             [--test_option <option_number> [<option_payload>]]

            Send a coap request. The generated coap request is designed to target the ns_coap_node project,
            allowing the NCP device to get/set the state of LaunchPad LEDs via the target's "led" CoAP resource.

            Parameters:
                ipv6 address:      Destination address for coap request. Multicast addresses are not supported.
                coap request code: Specify get, put, or post as the CoAP request code
                coap request type: Specify con (confirmable) or non (non-confirmable) as the CoAP request type.
                uri_path:          Specify the path of the URI resource. Specify led to target the ns_coap_node
                                   LED resource.
                --led_state:       Specify --led_state followed by the target LED (r or RLED or g for GLED) and
                                   state to set the LED (0 for off, 1 for on). Only valid for put or post requests.
                --test_option:     Optional argument to add an additional option to the request. Specify
                                   --test_option followed by an option number and option payload. See RFC7252 for
                                   details on CoAP options.

            Examples:
                Get request (confirmable):
                    > coap fdde:ad00:beef:0:558:f56b:d688:799 get con led

                Get request (nonconfirmable):
                    > coap fdde:ad00:beef:0:558:f56b:d688:799 get non led

                Post request (set RLED to on state)
                    > coap fdde:ad00:beef:0:558:f56b:d688:799 post con led --led_state r 1

                Get request with test option:
                    > coap fdde:ad00:beef:0:558:f56b:d688:799 get con led --test_option 3 hostname
        """
        router_state = self.prop_get_value(SPINEL.PROP_NET_STATE)
        if router_state < 5:
            print("Error: Device must be in join state 5 (Successfully joined and operational) to process coap commands")
            return

        params = line.split(" ")
        if len(params) < 4:
            print("Invalid number of parameters")
            return

        try:
            value = self.prop_get_value(SPINEL.PROP_IPV6_ADDRESS_TABLE)
            ipv6AddrTableList = self._parse_ipv6addresstable_property(value)
            srcIPAddress = "None"
            for i in range(0,len(ipv6AddrTableList)):
                if('0xFE80' not in str(ipv6AddrTableList[i]["ipv6Addr"])):
                    srcIPAddress = str(ipv6AddrTableList[i]["ipv6Addr"])
                    break

            if srcIPAddress == "None":
                print("Cannot perform CoAP request as device does not have a valid source IP address")
                return

            coap_req = None
            coap_confirm = None
            addr = params[0]
            if params[2] == 'con':
                coap_confirm = ipv6.COAP_TYPE_CON
            elif params[2] == 'non':
                coap_confirm = ipv6.COAP_TYPE_NON
            else:
                print("Invalid CoAP request type")
                return
            uri_path = params[3]

            option_list = []
            led_target = None
            led_state = None
            if len(params) > 4:
                for i in range(4, len(params)):
                    if params[i] == '--test_option' and len(params) >= (i + 1):
                        option_payload = None
                        if len(params) >= (i+2):
                            option_payload = params[i+2].encode('utf-8')
                        option_list.append(ipv6.CoAPOption(int(params[i+1]), option_payload))
                    if params[i] == '--led_state' and len(params) >= (i + 2):
                        if params[i+1] == 'r':
                            led_target = COAP_RLED_ID
                        elif params[i+1] == 'g':
                            led_target = COAP_GLED_ID
                        else:
                            print("Invalid LED target, must be g or r")
                            return

                        if params[i+2] == '0':
                            led_state = 0
                        elif params[i+2] == '1':
                            led_state = 1
                        else:
                            print("Invalid LED state, must be 0 or 1")
                            return

            if params[1] == "get":
                coap_req = self.ipv6_factory.build_coap_request(srcIPAddress, addr, coap_confirm,
                    ipv6.COAP_METHOD_CODE_GET, uri_path, option_list, led_target, led_state)
            elif params[1] == "put" or params[1] == "post":
                if params[1] == "put":
                    coap_req = self.ipv6_factory.build_coap_request(srcIPAddress, addr, coap_confirm,
                        ipv6.COAP_METHOD_CODE_PUT, uri_path, option_list, led_target, led_state)
                else:
                    coap_req = self.ipv6_factory.build_coap_request(srcIPAddress, addr, coap_confirm,
                        ipv6.COAP_METHOD_CODE_POST, uri_path, option_list, led_target, led_state)
            else:
                print("Invalid CoAP request code")
                return

            if coap_req is not None:
                self.wpan_api.ip_send(coap_req)
                # Let handler print result
        except:
            print("Fail")
            print(traceback.format_exc())

    #Helper util function to parse received PROP_IPV6_ADDRESS_TABLE property info
    def _parse_ipv6addresstable_property(self, propIPv6AddrTabInfo):
        """
        Internal utility function to convert IPv6 Addr Info into structure
        Returns a list of dictionary of IPv6 Address Table Entry
        Each Disctionary entry has ipv6Addr, prefixLen, validLifeTime and prefferedLifeTime
        """
        ipv6AddrTableList = []

        try:
            # 2 bytes = length of structure; 16 bytes IPv6 address; 1 byte = prefix len ; 4 bytes = valid lifetime; 4 bytes = preferred lifetime
            size = 0x1B

            # break the byte stream into different structure record
            addrStructs = [propIPv6AddrTabInfo[i:i + size] for i in range(0, len(propIPv6AddrTabInfo), size)]

            # parse each structure record as ipaddress; prefix_len; valid_lifetime; preferred_lifetime

            for addrStruct in addrStructs:
                ipv6AddrTableEntry = {}
                addr = addrStruct[2:18] # 6
                ipv6AddrTableEntry["ipv6Addr"] = ipaddress.IPv6Address(addr)
                ipv6AddrTableEntry["prefixLen"] = int.from_bytes(addrStruct[18:19], "little", signed=False) # C
                ipv6AddrTableEntry["validLifeTime"] = int.from_bytes(addrStruct[19:23], "little", signed=False) # L
                ipv6AddrTableEntry["prefferedLifeTime"] = int.from_bytes(addrStruct[23:27], "little", signed=False) # L
                ipv6AddrTableList.append(ipv6AddrTableEntry)
        except Exception as es:
            print("Exception raised during Parsing IPv6Address Table")
            print(es)
            return([])

        return(ipv6AddrTableList)

    # for IPV6 properties
    def do_numconnected(self, line):
        """
        Displays the number of Wi-SUN FAN nodes which have joined to the Wi-SUN FAN border router device.

        > numconnected
        2
        Done
        """
        try:
            # Only valid for BR
            if self.prop_get_value(SPINEL.PROP_NET_ROLE) != 0:
                print("Error: Device role must be Border Router to process this command")
                return
            router_state = self.prop_get_value(SPINEL.PROP_NET_STATE)
            if router_state < 5:
                print("Error: Device must be in join state 5 (Successfully joined and operational) to process this command")
                return

            value = self.prop_get_value(SPINEL.PROP_NUM_CONNECTED_DEVICES)
            if value is None:
                print("Error: Could not retrieve connected devices from embedded device")
                return
            print(value)
            print("Done")
        except:
            print("Fail")
            print(traceback.format_exc())

    def do_connecteddevices(self, line):
        """
        Displays the list of Wi-SUN FAN router nodes which have joined to the Wi-SUN FAN border router device.

        > connecteddevices
        List of connected devices currently in routing table:
        fd00:7283:7e00:0:212:4b00:1ca1:727a
        fd00:7283:7e00:0:212:4b00:1ca6:17ea
        Done
        """
        try:
            # Only valid for BR
            if self.prop_get_value(SPINEL.PROP_NET_ROLE) != 0:
                print("Error: Device role must be Border Router to process this command")
                return
            router_state = self.prop_get_value(SPINEL.PROP_NET_STATE)
            if router_state < 5:
                print("Error: Device must be in join state 5 (Successfully joined and operational) to process this command")
                return

            num_addrs = 0
            last_block = False
            print("List of connected devices currently in routing table:")
            while (not last_block):
                value = self.prop_get_value(SPINEL.PROP_CONNECTED_DEVICES)
                if value is None:
                    print("Error: Could not retrieve connected devices from embedded device")
                    return
                # Break the byte stream into different entries
                last_block = True if (value[0] >> 7) == 1 else False
                addrEntries = [value[i:i + IPV6_ADDR_LEN] for i in range(1, len(value), IPV6_ADDR_LEN)]
                for addrEntry in addrEntries:
                    print(ipaddress.IPv6Address(addrEntry))
                    num_addrs += 1

            if (num_addrs == 0):
                print("No nodes currently in routing table.")
            else:
                print("Number of connected devices: %d" % num_addrs)
            print("Done")
        except:
            print("Fail")
            print(traceback.format_exc())

    def do_dodagroute(self, line):
        """
        Displays the full routing path to a device with a specified IPv6 address. Also displays the path cost.

        > dodagroute fd00:7283:7e00:0:212:4b00:10:50d0
        Path cost: 2
        fd00:7283:7e00:0:212:4b00:10:50d4
        fd00:7283:7e00:0:212:4b00:1ca1:758e
        fd00:7283:7e00:0:212:4b00:10:50d0
        Done
        """
        try:
            params = line.split(" ")
            try:
                ipaddr = ipaddress.IPv6Address(params[0])
            except:
                print("Error: Invalid IPv6 address")
                return

            # Only valid for BR
            if self.prop_get_value(SPINEL.PROP_NET_ROLE) != 0:
                print("Error: Device role must be Border Router to process this command")
                return
            router_state = self.prop_get_value(SPINEL.PROP_NET_STATE)
            if router_state < 5:
                print("Error: Device must be in join state 5 (Successfully joined and operational) to process this command")
                return

            set_value = self.prop_set_value(SPINEL.PROP_DODAG_ROUTE_DEST, ipaddr.packed, str(len(ipaddr.packed)) + 's')
            value = self.prop_get_value(SPINEL.PROP_DODAG_ROUTE)
            if set_value is None or value is None or len(value) == 0:
                print("Error: Could not retrieve dodag route to selected embedded device")
                return

            path_cost = value[0]
            if path_cost == 0:
                print("No path to device with specified IPv6 address")
                return

            # Break the byte stream into different entries
            print("Path cost: %d" % value[0])
            addrEntries = [value[i:i + IPV6_ADDR_LEN] for i in range(1, len(value), IPV6_ADDR_LEN)]
            for addrEntry in addrEntries:
                print(ipaddress.IPv6Address(addrEntry))
            print("Done")
        except:
            print("Fail")
            print(traceback.format_exc())

    def do_ipv6addresstable(self, line):
        """
        ipv6addresstable

            Display the Globally Unique DHCP address and Link Local Adress along with
            prefix length, valid lifetime and preferred lifetime

            >ipv6addresstable

            fd00:7283:7e00:0:212:4b00:1ca1:9463; prefix_len = 64; valid_lifetime = 84269; preferred_lifetime = 41069
            fe80::212:4b00:1ca1:9463; prefix_len = 64; valid_lifetime = 4294967295; preferred_lifetime = 4294967295
            Done
        """
        value = self.prop_get_value(SPINEL.PROP_IPV6_ADDRESS_TABLE)
        ipv6AddrTableList = self._parse_ipv6addresstable_property(value)
        for i in range(0,len(ipv6AddrTableList)):
            print(str(ipv6AddrTableList[i]["ipv6Addr"]) + "; prefix_len = " + str(ipv6AddrTableList[i]["prefixLen"]) + "; valid_lifetime = " + str(ipv6AddrTableList[i]["validLifeTime"]) + "; preferred_lifetime = " + str(ipv6AddrTableList[i]["prefferedLifeTime"]))

        print("Done")

    #reset cmd
    def do_reset(self, line):
        """
        reset

            Reset the NCP.

            > reset
        """
        self.wpan_api.cmd_reset()
        self.clear_routing_table()

    def do_nverase(self, line):
        """
        nverase

            Erase the NV memory on NCP.

            > nverase
        """
        self.wpan_api.cmd_nverase()

    # other definitions

    def _notify_simulator(self):
        """
        notify the simulator that there are no more UART data for the current command.
        """
        OT_SIM_EVENT_POSTCMD = 4

        message = struct.pack('=QBHB', 0, OT_SIM_EVENT_POSTCMD, 1,
                              int(self.nodeid))
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(self._addr)
        sock.sendto(message, self._simulator_addr)
        sock.close()

    def postcmd(self, stop, line):
        if self.VIRTUAL_TIME:
            self._notify_simulator()
        return stop


def parse_args():
    """" Send spinel commands to initialize sniffer node. """
    args = sys.argv[1:]

    opt_parser = optparse.OptionParser(usage=optparse.SUPPRESS_USAGE)
    opt_parser.add_option("-u",
                          "--uart",
                          action="store",
                          dest="uart",
                          type="string")
    opt_parser.add_option("-b",
                          "--baudrate",
                          action="store",
                          dest="baudrate",
                          type="int",
                          default=DEFAULT_BAUDRATE)
    opt_parser.add_option("--rtscts",
                          action="store_true",
                          dest="rtscts",
                          default=False),
    opt_parser.add_option("-p",
                          "--pipe",
                          action="store",
                          dest="pipe",
                          type="string")
    opt_parser.add_option("-s",
                          "--socket",
                          action="store",
                          dest="socket",
                          type="string")
    opt_parser.add_option("-n",
                          "--nodeid",
                          action="store",
                          dest="nodeid",
                          type="string",
                          default="1")
    opt_parser.add_option("-q", "--quiet", action="store_true", dest="quiet")
    opt_parser.add_option("-v",
                          "--verbose",
                          action="store_true",
                          dest="verbose")
    opt_parser.add_option("-d",
                          "--debug",
                          action="store",
                          dest="debug",
                          type="int",
                          default=CONFIG.DEBUG_ENABLE)
    opt_parser.add_option("--vendor-path",
                          action="store",
                          dest="vendor_path",
                          type="string")

    return opt_parser.parse_args(args)


def main():
    """ Top-level main for spinel-cli tool. """
    (options, remaining_args) = parse_args()

    if options.debug:
        CONFIG.debug_set_level(options.debug)

    # Obtain the vendor module path, if provided
    if not options.vendor_path:
        options.vendor_path = os.environ.get("SPINEL_VENDOR_PATH")

    if options.vendor_path:
        options.vendor_path = os.path.abspath(options.vendor_path)
        vendor_path, vendor_module = os.path.split(options.vendor_path)
        sys.path.insert(0, vendor_path)
    else:
        vendor_module = "vendor"

    # Set default stream to pipe
    stream_type = 'p'
    stream_descriptor = "../../examples/apps/ncp/ot-ncp-ftd " + options.nodeid

    if options.uart:
        stream_type = 'u'
        stream_descriptor = options.uart
    elif options.socket:
        stream_type = 's'
        stream_descriptor = options.socket
    elif options.pipe:
        stream_type = 'p'
        stream_descriptor = options.pipe
        if options.nodeid:
            stream_descriptor += " " + str(options.nodeid)
    else:
        if len(remaining_args) > 0:
            stream_descriptor = " ".join(remaining_args)

    stream = StreamOpen(stream_type, stream_descriptor, options.verbose,
                        options.baudrate, options.rtscts)
    try:
        vendor_ext = importlib.import_module(vendor_module + '.vendor')
        cls = type(vendor_ext.VendorSpinelCliCmd.__name__,
                   (SpinelCliCmd, vendor_ext.VendorSpinelCliCmd), {})
        shell = cls(stream, nodeid=options.nodeid, vendor_module=vendor_module)
        #print(" no exception occurred ")
    except ImportError:
        shell = SpinelCliCmd(stream,
                             nodeid=options.nodeid,
                             vendor_module=vendor_module)
        #print(" in exception")

    try:
        shell.cmdloop()
    except KeyboardInterrupt:
        CONFIG.LOGGER.info('\nCTRL+C Pressed')

    if shell.wpan_api:
        shell.wpan_api.stream.close()


if __name__ == "__main__":
    main()
