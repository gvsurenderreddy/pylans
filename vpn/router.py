#! /usr/bin/env python
# Copyright (C) 2010  Brian Parma (execrable@gmail.com)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#
# router.py
#
#TODO another way to check if an id is known, if there are going to be multiple networks using the same router...
# #### each network has it's own adapter/router
#TODO make tun/tap both work, selectable
#TODO starting/stopping router doesn't owrk right
#     * clear peer list when going offline, if we go back online other peer things we are still connected
#     * should we keep the peer list and just refresh/let it timeout, or clear it?

import logging
import random
from struct import pack, unpack
from tuntap import TunTap
from twisted.internet import reactor, defer
from twisted.internet.protocol import DatagramProtocol
#from vpn import settings
from vpn.crypto import Crypter
from util.event import Event
from vpn.peers import PeerManager
from vpn.pinger import Pinger
from vpn.sessions import SessionManager
from vpn import settings
import util

logger = logging.getLogger(__name__)

class UDPPeerProtocol(DatagramProtocol):
    '''Protocol or sending/receiving data to peers'''

    def send(self, data, address):
        '''Send data to address'''
        try:
            self.transport.write(data, address)
            logger.debug('sending {1} bytes on UDP port to {0}'.format(address, len(data)))
        except Exception, e:
            logger.warning('UDP send threw exception:\n  {0}'.format(e))
            ##TODO this is here because UDP socket fills up and just dies
            # but it's UDP so we can drop packets

    def datagramReceived(self, data, address):
        '''Called by twisted when data is received from address'''
        self.router.recv_udp(data, address)
        logger.debug('received {1} bytes on UDP port from {0}'.format(address, len(data)))

    def connectionRefused(self):
        logger.debug('connectionRefused on UDP port')


class Router(object):
    '''The router object handles all the traffic between the virtual tun/tap
    device and the peers.  All traffic flows through the router, where it is
    filtered (encryption/decryption) and sent to its destination or a handler
    for special packets.

    Packet format: TBD'''
    VERSION = pack('H', 1)

    TIMEOUT = 5 # 5s
    # packet types
    #HANDSHAKE = 0
    ENCODED = 0x80
    DATA = 1
#    DATA_BROADCAST = 2
    DATA_RELAY = 2
    ACK = 3
    RELAY = 4

    #USER = 0x80

    def __init__(self, network, proto=None, tuntap=None):
        if tuntap is None:
            mode = network.adapter_mode
            tuntap = TunTap(self, mode)
        if proto is None:
            proto = UDPPeerProtocol()

        logger.info('Initializing router in {0} mode.'.format('TAP' if tuntap.is_tap else 'TUN'))

        self.handlers = {}
        self._requested_acks = {}

        self.network = util.get_weakref_proxy(network)
        #self.filter = Crypter(network.key)
        proto.router = util.get_weakref_proxy(self)
        self.sm = SessionManager(self)
        self.pm = PeerManager(self)

        # filterz
        #self._filterator = PacketFilter(self)
        #self.filter = self._filterator.filter
        #self.unfilter = self._filterator.unfilter

        self.addr_map = self.pm.addr_map
        self.relay_map = self.pm.relay_map

        # move this out of router?
        self.pinger = Pinger(self)

        self._proto = proto
        self._tuntap = tuntap
        self._port = None

        # move out of router?
        import bootstrap
        self._bootstrap = bootstrap.TrackerBootstrap(network)

        # add handler for message acks
        self.register_handler(self.ACK, self.handle_ack)

    def get_my_address(self):
        '''Get interface address (IP or MAC), return a deferred.
        Override'''
        pass

    def start(self):
        '''Start the router.  Starts the tun/tap device and begins listening on
        the UDP port.'''
        # start tuntap device
        self._tuntap.start()

        # configure tun/tap address
        d = self._tuntap.configure_iface(self.network.virtual_address)

        # set mtu (if possible)
        mtu = settings.get_option(self.network.name + '/' + 'set_mtu', None)
        if mtu is None:
            mtu = settings.get_option(self.network.name + '/' + 'set_mtu', None)
        if mtu is not None:
            d.addCallback(self._tuntap.set_mtu, mtu)

        # start UDP listener
        self._port = reactor.listenUDP(self.network.port, self._proto)

        # get addresses
        d.addCallback(self.get_my_address)

        logger.info('router started, listening on UDP port {0}'.format(self._port))

        def start_connections(*x):
            self._bootstrap.start()
            self.pinger.start() #TODO make this more modular
            #reactor.callLater(1, util.get_weakref_proxy(self.try_old_peers))

        # when the adapter is up, start network tools
        d.addCallback(start_connections)

    def stop(self):
        '''Stop the router.  Stops the tun/tap device and stops listening on the
        UDP port.'''
        self.pinger.stop()
        self._bootstrap.stop()
        self._tuntap.stop()
        # bring down iface?
        self.pm.clear()
        if self._port is not None:
            self._port.stopListening()
            self._port = None

        logger.info('router stopped')

    #def try_old_peers(self):
    #    '''Try to connect to addresses that were peers in previous sessions.'''
    #
    #    logger.info('trying to connect to previously known peers')
    #
    #    for pid in self.network.known_addresses:
    #        if pid not in self.pm:
    #            addrs = self.network.known_addresses[pid]
    #            self.pm.try_register(addrs)
    #
    #    # re-schedule
    #    reactor.callLater(60*5, util.get_weakref_proxy(self.try_old_peers))

    def relay(self, data, dst):
        if dst in self.pm:
            logger.debug('relaying packet to {0}'.format(repr(dst)))
            self.send_udp(data, self.pm[dst].address)


    def send(self, type, data, dst, ack=False, id=0, ack_timeout=None, clear=False):
        '''Send a packet of type with data to address.  Address should be an id
        if the peer is known, since address tuples aren't unique with relaying'''
        # shortcut for data, to speed up teh BWs
        if type == self.DATA:
            dst_id = dst[1]
            dst = dst[0]
            # encode
            data = self.sm.encode(dst_id, data)
            #type |= self.ENCODED
            # pack
            data = pack('!2H', type, id) + dst_id + self.pm._self.id + data
            # send
            return self.send_udp(data, dst)

        elif dst in self.sm:
            dst_id = dst
            dst = self.sm.session_map[dst]
        elif dst in self.pm:
            pi = self.pm[dst]
            dst_id = pi.id
            dst = pi.address

        elif isinstance(dst, tuple): # address tuple (like for greets)
            pi = self.pm.get(dst)
            if pi is not None:
                dst_id = pi.id
                if data != '' and not clear:
                    data = self.sm.encode(dst_id, data)
            else:
                dst_id = '\x00'*16 # non-routable

        #elif dst in self.pm: # known peer dst
        #    peer = self.pm[dst]
        #    dst_id = peer.id
        #    dst = peer.address

        elif dst in self.sm.session_map: # peerless session (during handshake)
            dst_id = dst
            dst = self.sm.session_map[dst]

        else: # unknown peer dst TODO: this should return an erroring deferred?
            logger.error('cannot send to unknown dest {0}'.format(repr(dst)))
            return #todo throw exception

        if ack or id > 0: # want ack
            if id == 0:
                id = random.randint(0, 0xFFFF)
            d = defer.Deferred()
            timeout = ack_timeout if ack_timeout is not None else self.TIMEOUT
            timeout_call = reactor.callLater(timeout, util.get_weakref_proxy(self._timeout), id)
            self._requested_acks[id] = (d, timeout_call)
        else:
            d = None


        if data != '' and not clear and dst_id in self.sm:
            data = self.sm.encode(dst_id, data)
            logger.info('encoding packet {0}'.format(type))
            type |= self.ENCODED
        #else:
            #logger.critical('trying to send encrypted packets but no associated session')
            # TODO assert clear == true, because we can't encrypt here

        data = pack('!2H', type, id) + dst_id + self.pm._self.id + data

        #TODO exception handling for bad addresses
        self.send_udp(data, dst)

        return d

    def handle_ack(self, type, data, address, src):
        id = unpack('!H', data)[0]
        logger.debug('got ack with id {0}'.format(id))

        if id in self._requested_acks:
            d, timeout_call = self._requested_acks[id]
            del self._requested_acks[id]
            timeout_call.cancel()
            d.callback(id)

    def _timeout(self, id):
        if id in self._requested_acks:
            d = self._requested_acks[id][0]
            del self._requested_acks[id]
            logger.info('ack timeout')
            d.errback(Exception('call {0} timed out'.format(id)))
#            d.errback(id)
        else:
            logger.info('timeout called with bad id??!!?')

    def send_udp(self, data, address):
        #data = self.filter.encrypt(data)
        self._proto.send(data, address)

    def send_packet(self, packet):
        '''Got a packet from the tun/tap device that needs to be sent out'''
        pass

    def recv_udp(self, data, address):
        '''Received a packet from the UDP port.
        Parse it and send it on its way.
        Data types get special treatment to reduce overhead.'''

        dst = data[4:20]

        # ours?
        if dst == self.pm._self.id or dst == '\x00'*16:
            pt = unpack('!H', data[:2])[0]
            src = data[20:36]

            if pt == self.DATA:
                #src = self.pm.get_by_address(address)
                packet = self.sm.decode(src, data[36:])
                self.recv_packet(packet, src)

            else:

                if (pt & self.ENCODED) > 0:
                    pt = pt & 0x7F
                    packet = self.sm.decode(src, data[36:])
                    #logger.info('got encoded packet {0}'.format(pt))
                else:
                    packet = data[36:]

                # should i use 1byte type + 3byte id, or 2byte type + 2byte id TODO
                id = unpack('!H', data[2:4])[0]

                # get dst and src 128-bit ids
                # should i mandate this or let handlers decide? how to do routing
                # otherwise
                #dst = data[4:20]
                #src = data[20:36]

                if pt in self.handlers:
                    # need to check if this is from a known peer?
                    self.handlers[pt](pt, packet, address, src)
                if id > 0: # ACK requested TODO: ack request from unknown peer fails
                    logger.debug('sending ack')
                    # ack to unknown sources? TODO
                    self.send(self.ACK, data[2:4], src, clear=True)
                logger.debug('handling {0} packet from {1}'.format(pt, src.encode('hex')))

        # nope!
        else:
            return self.relay(data, dst)


    def recv_packet(self, packet, address):
        '''Got a data packet from a peer, need to inject it into tun/tap'''
        pass

    def register_handler(self, type, callback):
        '''Register a handler for a specific packet type.  Handles will be
        called as 'callback(type, data, address, src_id)'.'''

        logger.debug('registering packet handler for packet type: {0}'.format(type))

        if type in self.handlers:
            self.handlers[type] += callback

        else:
           self.handlers[type] = Event()
           self.handlers[type] += callback

    def unregister_handler(self, type, callback):
        '''Remove a registered handler for a specific packet type.'''

        logger.debug('unregistering packet handler for packet type: \
                     {0}'.format(type))

        if type in self.handlers:
            self.handlers[type] -= callback

class TapRouter(Router):
    addr_size = 6

    SIGNATURE = 'PVA'+Router.VERSION

    def get_my_address(self, *x):
        '''Get interface address (IP/MAC)'''

        d = defer.Deferred()
        def do_ips(ips=None):
            '''get the IP/mac addresses'''
            if ips is None:
                ips = self._tuntap.get_ips()
            if len(ips) > 0:
                if self.pm._self.vip_str not in ips:
                    logger.critical('TAP addresses ({0}) don\'t contain \
                                    configured address ({1}), taking address\
                                    from adapter ({2})'.format(ips,
                                            self.pm._self.vip_str, ips[0]))
                    self.pm._self.vip = util.encode_ip(ips[0])
            else:
                logger.critical('TAP adapater has no addresses')

            # get mac addr
            self.pm._self.addr = self._tuntap.get_mac()
            self.pm._update_pickle()

            reactor.callLater(0, d.callback, ips)

        # Grap VIP, so we display the right one
        ips = self._tuntap.get_ips()
        if len(ips) == 1 and ips[0] == '0.0.0.0': # interface not ready yet?
            logger.warning('Adapter not read, delaying...')
            reactor.callLater(3, do_ips)
        else:
            do_ips(ips)

        return d

    def send_packet(self, packet):
        '''Got a packet from the tun/tap device that needs to be sent out'''

        dst = packet[0:self.addr_size]

        # if ip in peer list
        if dst in self.addr_map:
            # encrypt packet
            #packet = self.sm.encode(self.addr_map[dst][1], packet)
            self.send(self.DATA, packet, self.addr_map[dst])

        # or if it's a broadcast
        elif self._tuntap.is_broadcast(dst):
            #logger.debug('sending broadcast packet')
            for addr in self.addr_map.values():
                # encrypt
                #epacket = self.sm.encode(addr[1], packet)
                self.send(self.DATA, packet, addr)

        # if we don't have a direct connection...
        elif dst in self.relay_map:
            # encrypt packet
            #packet = self.sm.encode(self.relay_map[dst][1], packet)
            self.send(self.DATA, packet, self.relay_map[dst])
        else:
            logger.debug('got packet on wire to unknown destination: \
                         {0}'.format(dst.encode('hex')))

    def recv_packet(self, packet, src):
        '''Got a data packet from a peer, need to inject it into tun/tap'''
        #decrypt packet
        #try:
        #    #print 'dec',self.pm.get_by_address(address),(address)
        #    packet = self.sm.decode(src, packet)
        #except:
        #    logger.error('could not decrypt a packet')
        #    return

        dst = packet[0:self.addr_size]

        # is it ours?
        if dst == self.pm._self.addr or self._tuntap.is_broadcast(dst):
            self._tuntap.doWrite(packet)
            logger.debug('writing packet to TAP device')
        else:
            # no, odd
            self.send_packet(packet)
            logger.debug('got packet (encrypted) with different dest ip, relay packet?')



class TunRouter(Router):
    '''not currently in use'''
    addr_size = 4

    SIGNATURE = 'PVU'+Router.VERSION

    def get_my_address(self):
        '''Get interface address (IP)'''
        ips = self._tuntap.get_ips()
        if len(ips) > 0:
#            ips = [x[0] for x in ips] # if we return (addr,mask)
            if self.pm._self.vip_str not in ips:
                logger.critical('TUN addresses ({0}) don\'t contain configured address ({1}), taking address from adapter ({2})'.format(ips, self.pm._self.vip_str, ips[0]))
                self.pm._self.vip = util.encode_ip(ips[0])
                self.pm._self.addr = self.pm._self.vip
        else:
            logger.critical('TUN adapater has no addresses')
            self.pm._self.addr = self.pm._self.vip

        self.pm._update_pickle()

    def send_packet(self, packet):
        '''Got a packet from the tun/tap device that needs to be sent out'''
#        print 'tunk:\n',packet[0:14].encode('hex')
        dst = packet[0:self.addr_size]
#        prot = unpack('1B',packet[9])[0]

        # if ip in peer list
        if dst in self.addr_map:
            self.send(self.DATA, packet, self.addr_map[dst])
        else:
            logger.debug('got packet on wire to unknown destination: {0}'.format(dst.encode('hex')))

    def recv_packet(self, packet):
        '''Got a data packet from a peer, need to inject it into tun/tap'''
        # check?
        dst = packet[0:self.addr_size]

        if dst == self.pm._self.addr:
            self._tuntap.doWrite(packet)
        else:
            self.send_packet(packet)
            logger.debug('got packet with different dest ip, relay packet?')

def get_router(net, *args, **kw):
    if net.adapter_mode == 'TAP':
        return TapRouter(net, *args, **kw)
    else:
        return TunRouter(net, *args, **kw)
