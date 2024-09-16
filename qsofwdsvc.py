import logging
import logging.handlers
import os
import platform
import select
import socket
import struct
import sys
import yaml

LOG = logging.getLogger('qsofwd')
LOGF = os.path.join(os.path.abspath(os.path.dirname(__file__)),
                    'qsofwd_log.txt')
logging.basicConfig(level=logging.DEBUG,
                    handlers=[
                        logging.handlers.RotatingFileHandler(
                            LOGF,
                            maxBytes=10 << 20),
                        logging.StreamHandler()])


WSJTX_HEARTBEAT = 0

class WSJPacket:
    @classmethod
    def parse(cls, message):
        magic, schema, number = struct.unpack('>III', message[:3 * 4])
        if magic != 0xADBCCBDA:
            LOG.error('Invalid magic: %08x', magic)
            return
        if schema != 2:
            LOG.error('Invalid schema %i', schema)
            return
        ident = cls.parse_string(message, 3 * 4)
        if number == 0:
            props = cls.parse_type_0(message, 4 * 4 + len(ident))
        else:
            props = {}
        props.update({'type': number, 'ident': ident})
        return cls(**props)

    @classmethod
    def parse_type_0(cls, message, offset):
        max_schema = struct.unpack('>I', message[offset:offset + 4])
        offset += 4
        version = cls.parse_string(message, offset)
        offset += len(version)
        revision = cls.parse_string(message, offset)
        return dict(version=version,
                    revision=revision, max_schema=max_schema)

    @staticmethod
    def parse_string(buffer, offset):
        size, = struct.unpack('>I', buffer[offset:offset + 4])
        return buffer[offset + 4:offset + 4 + size].decode()

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class WSJTXSource:
    def __init__(self, ident, dest):
        self.ident = ident
        self.dest = dest
        self.proxysock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


class QSOForwarder:
    def _parse_config(self, config_file):
        with open(config_file) as f:
            self._config = yaml.load(f, Loader=yaml.SafeLoader)
        LOG.info('Loaded config %s', config_file)
        self._last_config = os.stat(config_file).st_mtime
        try:
            level = getattr(logging, self._config.get('loglevel', 'WARNING'))
            LOG.setLevel(level)
        except AttributeError:
            LOG.error('Unknown loglevel %r', self._config['loglevel'])

    @property
    def config(self):
        config_file = os.path.join(os.path.dirname(
            os.path.abspath(__file__)), 'qsofwd.yaml')
        s = os.stat(config_file)
        if s.st_mtime > self._last_config:
            try:
                self._parse_config(config_file)
            except Exception as e:
                LOG.exception('Failed to load config: %s', e)
        return self._config

    def setup(self):
        self._last_config = 0
        self._config = {}
        self.inbound = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.inbound.setblocking(0)
        source = self.config.get('source', {})
        source_addr = (source.get('host', '0.0.0.0'),
                       source.get('port', 2237))
        self.inbound.bind(source_addr)
        LOG.debug('Listening on %s:%i' % source_addr)
        self.sources = {}

    def run_one(self):
        proxysocks = {src.ident: src.proxysock
                      for src in self.sources.values()}
        sockets = [self.inbound] + list(proxysocks.values())
        readable, _, _ = select.select(sockets, [], sockets, 0.25)

        # WSJTX -> consumers
        if self.inbound in readable:
            data, addr = self.inbound.recvfrom(65535)
            p = WSJPacket.parse(data)
            try:
                source = self.sources[p.ident]
                # Always keep the endpoint updated in case of close/reopen
                source.dest = addr
            except KeyError:
                # Only record new sources when they heartbeat. This is not
                # necessary but allows us to log the ident/version when we
                # see them once
                if p.type == WSJTX_HEARTBEAT:
                    LOG.info('New source found %s:%s - %r %r %r',
                             addr[0], addr[1], p.ident, p.version, p.revision)
                    self.sources[p.ident] = source = WSJTXSource(p.ident, addr)
                else:
                    source = None

            LOG.info('Received type %i from %s', p.type, p.ident)

            for dest in self.config.get('destinations', []):
                # Proxy to all the configured destination consumers
                host = dest.get('host', '127.0.0.1')
                name = dest.get('name', '%s:%s' % (host, dest['port']))
                try:
                    source.proxysock.sendto(data, (host, dest['port']))
                except AttributeError:
                    # No source yet
                    pass
                except socket.error as e:
                    LOG.warning('Unable to send to %s on port %i: %s' % (
                        name, dest['port'], e))

        # Reply consumer -> WSJTX
        for ident, proxysock in proxysocks.items():
            if proxysock not in readable:
                continue
            try:
                data, addr = proxysock.recvfrom(65535)
            except ConnectionResetError:
                data = None
                continue

            # Parse the packet to determine the desired WSJ instance and
            # send it there
            p = WSJPacket.parse(data)
            try:
                source = self.sources[p.ident]
                self.inbound.sendto(data, source.dest)
            except KeyError:
                # Specified an unknown ident (not likely)
                LOG.warning('Message from client %s for unknown source %s',
                            addr[0], p.ident)
            except socket.error as e:
                LOG.warning('Unable to send to %s: %s' % (source.dest, e))
            else:
                LOG.info('Message from client %s, sending to %s host %s:%i',
                            addr[0], source.ident, *source.dest)


class POSIXQSOForwarder(QSOForwarder):
    @classmethod
    def main(cls):
        forwarder = cls()
        forwarder.setup()
        while True:
            try:
                forwarder.run_one()
            except KeyboardInterrupt:
                break
            forwarder.config


if platform.system() == 'Windows':
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class WinQSOFWDService(win32serviceutil.ServiceFramework, QSOForwarder):
        _svc_name_ = 'QSOFWDService'
        _svc_display_name_ = 'QSO Forward Service'
        _svc_description_ = 'QSO Forwarding Service'

        def __init__(self, args):
            super().__init__(args)
            self.event = win32event.CreateEvent(None, 0, 0, None)

        def SvcDoRun(self):
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)

            self.setup()

            LOG.info('Starting')
            while True:
                result = win32event.WaitForSingleObject(self.event, 1)
                if result == win32event.WAIT_OBJECT_0:
                    LOG.info('Exiting')
                    break
                self.run_one()

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.event)

        def GetAcceptedControls(self):
            result = win32serviceutil.ServiceFramework.GetAcceptedControls(self)
            result |= win32service.SERVICE_ACCEPT_PRESHUTDOWN
            return result

        @classmethod
        def main(cls):
            if len(sys.argv) == 1:
                servicemanager.Initialize()
                servicemanager.PrepareToHostSingle(cls)
                servicemanager.StartServiceCtrlDispatcher()
            else:
                win32serviceutil.HandleCommandLine(cls)
    forward_cls = WinQSOFWDService
else:
    forward_cls = POSIXQSOForwarder


if __name__ == '__main__':
    forward_cls.main()
