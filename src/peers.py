# peers.py
# TODO rsa key exchange - new 'packet format'
# TODO local clients behind a firewall that use an external intermediary need a way to 
#     realize that they can DC using local addresses
# TODO introducer (bittorrent?)

from twisted.internet import reactor, defer
import cPickle as pickle
import event
import logging
import util
import uuid


logger = logging.getLogger(__name__)

class PeerInfo(object):
    '''Represents a peer connection'''   
    def __init__(self):
        self.id = 0                     # unique peer id
        self.name = 'wop'
        self.alias = None
        self.address = ('ip','port')
        self.direct_addresses = []
        self.addr = 0                   # can be vip or mac
        self.vip = 0                    # virtual ip
        self.is_direct = False          # is this peer direct connected?
        self.relays = 0
        self.relay_id = 0               # if not, who is the relay
        self.ping_time = 0              #
        self.timeouts = 0               # tracking ping timeouts
        
#    def update(self, peer):
#        self.name = peer.name
#        self.vip = peer.vip

    @property
    def vip_str(self):
        return util.decode_ip(self.vip)
        
    @property
    def addr_str(self):
        if len(self.addr) == 4:
            return util.decode_ip(self.addr)
        else:
            return util.decode_mac(self.addr)

class PeerManager(object):
    '''Manages peer connections'''
    MAX_REG_TRIES = 5
    MAX_PX_TRIES = 5
    REG_TRY_DELAY = 2
    PX_TRY_DELAY = 2

    # packet types
    REGISTER = 14
    REGISTER_ACK = 15
    PEER_XCHANGE = 16
    PEER_XCHANGE_ACK = 17
    PEER_ANNOUNCE = 18
    
    def __init__(self, router):
        self.peer_list = {}
        self.peer_map = {}
        self.addr_map = {}
        
        self._self = PeerInfo()
        self._self.id = router.network.id
        self._self.name = router.network.username
        self._self.vip = util.encode_ip(router.network.ip)
        self._self.addr = '\x00'*router.addr_size # temp fake mac?
        self._my_pickle = pickle.dumps(self._self,-1)

        self.router = router
        router.register_handler(self.PEER_XCHANGE, self.handle_px)
        router.register_handler(self.PEER_XCHANGE_ACK, self.handle_px_ack)
        router.register_handler(self.REGISTER, self.handle_reg)
        router.register_handler(self.REGISTER_ACK, self.handle_reg_ack)
        router.register_handler(self.PEER_ANNOUNCE, self.handle_announce)
        
        # Events
#        self.peer_added = Event()
#        self.peer_removed = Event()

    def _update_pickle(self):
        self._my_pickle = pickle.dumps(self._self,-1)
        
        # should announce my change to my peerz
        self.send_announce(self._self, None)
        

    def add_peer(self, peer):
        '''Add a peer connection'''
        if peer.id not in self.peer_list:
            if peer.relays > 0:
                peer.is_direct = False
                peer.relay_id = self[peer.address].id
                self.try_register(peer.direct_addresses)
            else:
                peer.is_direct = True
                peer.relay_id = None
                if peer.address not in peer.direct_addresses:
                    peer.direct_addresses.append(peer.address)

            self.peer_list[peer.id] = peer
#            self.ip_map[peer.vip] = peer.address
            self.addr_map[peer.addr] = peer.address
            
            # fire event
            event.emit('peer-added', self, peer)
            self.send_announce(peer)
#            self.peer_added(peer)
                        
    def remove_peer(self, peer):
        '''Remove a peer connection'''
        if peer.id in self.peer_list:
            del self.peer_list[peer.id]
#        if peer.id in self.relay_list[peer.id]:
#            del self.relay_list[peer.id]
#        if peer.vip in self.ip_map:
#            del self.ip_map[peer.vip]
        if peer.addr in self.addr_map:
            del self.addr_map[peer.addr]
            
            # fire event
#            self.peer_removed(peer)
            event.emit('peer-removed', self, peer)
            
    def get_by_name(self, name):
        '''Get a peer connection by peer name'''
        for p in self.peer_list.values():
            if p.name == name:
                return p
        return None

    def get_by_vip(self, vip):
        '''Get a peer connection by virtual ip'''
        for p in self.peer_list.values():
            if p.vip == vip:
                return p
        return None
 
    def get_by_addr(self, addr):
        '''Get a peer connection by mac or vip address'''
        for p in self.peer_list.values():
            if p.addr == addr:
                return p
        return None
            
    def get_by_address(self, address):
        '''Get a peer connection by real (ip,port)'''
        for p in self.peer_list.values():
            if p.address == address and p.is_direct:
                return p
        return None
        
    def _timeout(self, peer):
        logger.warning('peer {0} on network {1} timed out'.format(peer.name, self.router.network.name))
        self.remove_peer(peer)
           
           
    def update_peer(self, opi, npi):
        changed = False                
        if (opi.relays >= npi.relays and opi.address != npi.address):
            self.addr_map[opi.addr] = npi.address
            opi.address = npi.address
            opi.relays = npi.relays
            opi.is_direct = (opi.relays == 0)
            #relay id?
            changed = True
            logger.info('peer {0} relay changed.'.format(opi.id))
 
        if opi.addr != npi.addr:
            # check for collision?
            self.addr_map[npi.addr] = self.addr_map[opi.addr]
            del self.addr_map[opi.addr]
            opi.addr = npi.addr
            changed = True
            logger.info('peer {0} addr changed.'.format(opi.id))
            
        if opi.vip != npi.vip:
            # check for collision
            opi.vip = npi.vip
            changed = True
            logger.info('peer {0} vip changed.'.format(opi.id))
            
        if opi.name != npi.name:
            opi.name = npi.name
            changed = True            
            logger.info('peer {0} name changed.'.format(opi.id))

        if opi.direct_addresses != npi.direct_addresses:
            opi.direct_addresses = npi.direct_addresses
            changed = True
            logger.info('peer {0} good addresses changed.'.format(opi.id))

        if changed:
            # fire event
            event.emit('peer-changed', self, opi)
            self.send_announce(opi)
            
    
    ###### Announce Functions
    
    def send_announce(self, peer, address=None):
        '''Send an announce about peer to all known connections'''
        if peer.id != self._self.id:
            peer.relays += 1 # inc relay so routing works right
            peerkle = pickle.dumps(peer, -1)
            peer.relays -= 1
        else:
            #peerkle = pickle.dumps(peer, -1)
            peerkle = self._my_pickle
            
        if address is not None:
            self.router.send(self.PEER_ANNOUNCE, peerkle, address)        
            logger.info('sending announce about {0} to {1}'.format(peer.id, address))
        else:
            for p in self.peer_list.values():
                if p.id != peer.id:
                    self.router.send(self.PEER_ANNOUNCE, peerkle, p)
                    logger.info('sending announce about {0} to {1}'.format(peer.id, p.id))
        
    
    def handle_announce(self, type, packet, address, src_id):
        logger.info('received an announce packet from {0}'.format(address))
        pi = pickle.loads(packet)
        if pi.id != self._self.id:
            if pi.id not in self:
                if pi.relays == 0: #potential replacement for reg packets?
                    pi.address = address
                    self.add_peer(pi)
                    logger.info('announce from unknown peer {0}, adding and announcing self'.format(pi.name))
                else:
                    pi.address = address
                    self.add_peer(pi)
                    logger.info('announce for unknown peer {0}, trying to connect'.format(pi.name))
#                    self.try_register(pi)
            else:
                pi.address = address
                self.update_peer(self.peer_list[pi.id], pi)
    
    ###### Peer XChange Functions
    
    def try_px(self, peer):
        '''Initiate a peer exchange by sending a px packet.  The packet will be
        resent until an ack packet is recieved or MAX_PX_TRIES packets have been sent.
        This px packet includes the pickled peer list.'''
        
        logger.info('initiating a peer exchange with {0}'.format(peer.name))
        
        def send_px(i):
            if i <= self.MAX_PX_TRIES and peer.id not in self.peer_map:
                self.router.send(self.PEER_XCHANGE, pickle.dumps(self.peer_list,-1), peer)
                logger.debug('sending PX packet #{0}'.format(i))
 
                reactor.callLater(self.PX_TRY_DELAY, send_px, i+1)
                
        reactor.callLater(self.PX_TRY_DELAY, send_px, 0)

    def handle_px(self, type, packet, address, src_id):
        '''Handle a peer exchange packet.  Load the peer list with the px packet 
        and send an ack packet with own peer list.'''
        
        peer_list = pickle.loads(packet)
            
        # reply
        logger.info('received a PX packet, sending PX ACK')
        self.router.send(self.PEER_XCHANGE_ACK, pickle.dumps(self.peer_list,-1), src_id)
        self.parse_peer_list(self[src_id], peer_list)
            

    def handle_px_ack(self, type, packet, address, src_id):
        '''
            Handle a px ack packet by parsing incoming peer list
        '''
        
        logger.info('received a PX ACK packet')
        
        peer_list = pickle.loads(packet)
        self.parse_peer_list(self[src_id], peer_list)
    
    def parse_peer_list(self, from_peer, peer_list):
        '''Parse a peer list from a px packet'''
        
#        if from_peer.id not in self.peer_map:
        self.peer_map[from_peer.id] = peer_list

        for peer in peer_list.values():
            if peer.id != self._self.id:
                peer.relays += 1
                peer.address = from_peer.address

                if peer.id in self.peer_list:
                    self.update_peer(self.peer_list[peer.id],peer)
                else:
#TODO                    self.try_register(peer.address)
                    self.add_peer(peer)
                
            # generate map?
    
    
    
    ###### Peer Register Functions
    
    def try_register(self, addrs):
        '''Try to register self with a peer by sending a register packet
        with own peer info.  Will continue to send this packet until an 
        ack is received or MAX_REG_TRIES packets have been sent.'''
        
        if isinstance(addrs, tuple):
            addrs = [addrs]
        elif isinstance(addrs, PeerInfo):
            addrs = addrs.direct_addresses
        elif not isinstance(addrs, list):
            logger.error('try_register called with incorrect parameter: {0}'.format(addrs))
            return
    
        main_d = defer.Deferred()

        def try_address(err, j):
            if j < len(addrs):
                address = addrs[j]
                logger.info('initiating a register xchange with {0}'.format(address))

                if (address not in self.router.pm):
                    d = defer.Deferred()
                    
                    def send_register(i):
                        '''Send a register packet and re-queues self'''
                            
                        if i <= self.MAX_REG_TRIES and address not in self.router.pm:
                            logger.debug('sending REG packet #{0}'.format(i))
                            self.router.send(self.REGISTER, self._my_pickle, address)
                            reactor.callLater(self.REG_TRY_DELAY, send_register, i+1)
                        elif i > self.MAX_REG_TRIES:
                            logger.info('(reg) address {0} timed out'.format(address))
                            d.errback(Exception('address timed out'))
                        else: # address in PM
                            logger.debug('(reg) address {0} in PM.'.format(address))
                            d.callback(self.router.pm[address])

#                    reactor.callLater(self.REG_TRY_DELAY, send_register, 0)
                    # add callbacks in parallel
                    d.addCallbacks(main_d.callback, try_address, None, None, (j+1,), None)
                    send_register(0)
                else:
                    logger.debug('address {0} already in peer list'.format(address))
                    main_d.callback(self.router.pm[address])

            else:
                logger.info('no addresses passed to try_register responded.')
                main_d.errback(Exception('Could not establish connection with addresses.'))                
                
#            return err
        
        reactor.callLater(0, try_address, None, 0)
        main_d.addErrback(logger.info)
        return main_d #TODO this funky thing needs testing                

                        
    def handle_reg(self, type, packet, address, src_id):
        '''Handle incoming reg packet by adding new peer and sending ack.'''
        
        logger.info('received REG packet, sending ACK')
        
        pi = pickle.loads(packet)    
        if pi.id == self._self.id:
            # we sent a reg to ourself?
            pass
        elif pi.id not in self.peer_list:
            logger.info('received a register from a new peer {0}'.format(pi.name))
            pi.address = address
            self.add_peer(pi)
            self.try_px(pi)
        else:
            pi.address = address
#            pi.is_direct = (pi.relays == 0)
            self.update_peer(self.peer_list[pi.id], pi)

        self.router.send(self.REGISTER_ACK, self._my_pickle, address) # can't send to src_id, might not be known
                
        
    def handle_reg_ack(self, type, packet, address, src_id):
        '''Handle reg ack by adding new peer'''
        
        logger.info('received REG ACK packet')
        
        pi = pickle.loads(packet)

        if pi.id == self._self.id:
            # yea yea...
            pass
        elif pi.id not in self.peer_list:
            logger.info('received REG ACK packet from new peer {0}'.format(pi.name))
            pi.address = address
#            pi.is_direct = (pi.relays == 0)
            self.add_peer(pi)
            self.try_px(pi)
        else:
            pi.address = address
#            pi.is_direct = (pi.relays == 0)
            self.update_peer(self.peer_list[pi.id], pi)
                
                
    ###### Container Type Overloads
    
    def iterkeys(self):
        for peer in self.peer_list:
            yield peer
    
    def __iter__(self):
        return self.iterkeys()
    
    def __len__(self):
        return len(self.peer_list)
    
    def __getitem__(self, item):
        if isinstance(item, PeerInfo) and item in self:
            return item
            
        if isinstance(item, tuple):                     # address
            peer = self.get_by_address(item)
                
        elif isinstance(item, uuid.UUID):               # peer id
            peer = self.peer_list[item]
            
        elif isinstance(item, str):  # name
            peer = self.get_by_name(item)
            if peer is None:
                if len(item) == 4:
                    peer = self.get_by_vip(item)
                elif len(item) == 6:
                    peer = self.get_by_addr(item)
                elif len(item) == 16:
                    peer = self[uuid.UUID(bytes=item)]
        else:
            raise TypeError('Unrecognized key type')

        if peer is None:
            raise KeyError('Address {0} not in peer list.'.format(repr(item)))
        else:
            return peer

    def get(self, item, default=None):
        try:
            item = self[item]
        except KeyError:
            item = default
        return item
    
    def __contains__(self, item):
        if isinstance(item, PeerInfo):
            return item.id in self.peer_list        
        if isinstance(item, tuple):                     # address
            return self.get_by_address(item) is not None
        elif isinstance(item, uuid.UUID):               # peer id
            return (item in self.peer_list)
        elif isinstance(item, str):  # name or vip
            return (self.get(item) != None)
    

