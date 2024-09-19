import logging
import os
import platform
import select
import socket
import subprocess
import sys
import threading
import time
import yaml

LOG = logging.getLogger('rigproxy')

class RigProxy:
    """One instance of a serial->flrig proxy"""
    def __init__(self, config, rig, proxy):
        """
        config is the whole blob, rig is index into rigs, and proxy is
        index into that rig's list of proxy ports
        """
        self.config = config
        self.rig = rig
        self.proxy = proxy
        self.running = True
        self.log = logging.getLogger(self.config['rigs'][rig].get(
            'name', 'rig%i' % rig))
        self._flrig_sock = self._rigctlcom_sock = None
        self._rigctlcom = None
        self._thread = None

    @property
    def port(self):
        return self.config['rigs'][self.rig]['flrigport']

    @property
    def serial(self):
        return self.config['rigs'][self.rig]['proxies'][self.proxy]

    def stop(self):
        """Stops the proxy loop (and thread if running)"""
        self.running = False
        if self.thread:
            self.thread.join(1)
        self.log.info('Stoppped')

    def start(self):
        """Starts a thread to run the proxy loop"""
        self.thread = threading.Thread(target=self.thread_loop)
        self.thread.daemon = True
        self.thread.start()
        self.log.info('Started thread')

    def thread_loop(self):
        """The main meat of the proxy

        Try/wait to connect to flrig. Once flrig is connected, spawn the
        rigctlcom proxy, proxy data until one of them disconnects, then kill
        everything and try again.
        """
        while self.running:
            try:
                self._connect_flrig()
                if self._flrig_sock and not self._rigctlcom:
                    self._spawn_rigctlcom()
                self._proxy_loop()
            except ConnectionRefusedError:
                pass
            except socket.error as e:
                self.log.debug('Socket reset: %s', e)
                self._reset()
            except Exception as e:
                self.log.exception('Unexpected failure in loop: %s', e)
                self._reset()

    def _reset(self):
        """Reset all our sockets and kill our rigctlcom, if running"""
        if self._rigctlcom_sock:
            self.log.debug('Closing rigctlcom socket')
            self._rigctlcom_sock.close()
            self._rigctlcom_sock = None
        if self._rigctlcom:
            self.log.info('Stopping rigctlcom')
            self._rigctlcom.kill()
            self._rigctlcom.wait()
            self.log.debug('rigctlcom is dead')
            self._rigctlcom = None
        if self._flrig_sock:
            self.log.debug('Closing flrig socket')
            self._flrig_sock.close()
            self._flrig_sock = None

    def _connect_flrig(self):
        self._flrig_sock = socket.socket(socket.AF_INET,
                                         socket.SOCK_STREAM)
        self._flrig_sock.connect(('127.0.0.1', self.port))
        self.log.info('Connected to flrig on %i', self.port)

    def _spawn_rigctlcom(self):
        """Spawn a rigctlcom instance

        Allocates a random listen port and starts rigctlcom on the desired
        serial proxy connected to that port.
        """
        self.log.debug('Setting up rigctlcom')

        # Create a temporary listen socket on a random localhost port
        listen_sock = socket.socket(socket.AF_INET,
                                             socket.SOCK_STREAM)
        listen_sock.bind(('127.0.0.1', 0))
        listen_sock.listen()

        # Spawn rigctlcom to connect to that port
        cmd = [self.config['rigctlcom'], '-m4', '-S115200',
                '-R%s' % self.serial,
                '-r%s:%i' % listen_sock.getsockname()]
        self.log.debug('Spawning rigctlcom with %s', cmd)
        self._rigctlcom = subprocess.Popen(cmd)

        # Wait for rigctlcom to connect to us
        self._rigctlcom_sock, peer = listen_sock.accept()
        self.log.info('Rigctl connected')

        # We no longer need the parent listen socket
        listen_sock.close()

    def _proxy_loop(self):
        """Proxy data between rigctlcom and flrig until one dies"""
        socks = [self._rigctlcom_sock, self._flrig_sock]
        while True:
            r, _, x = select.select(socks, [], socks, 0.25)
            if self._rigctlcom_sock in r:
                data = self._rigctlcom_sock.recv(65536)
                self._flrig_sock.send(data)
                #self.log.debug('-> %i' % len(data))
            if self._flrig_sock in r:
                data = self._flrig_sock.recv(65536)
                self._rigctlcom_sock.send(data)
                #self.log.debug('<- %i' % len(data))


class RigProxies:
    """Multi-rig multi-proxy"""
    def __init__(self, config_file):
        self._config_file = config_file
        with open(config_file) as f:
            self.config = yaml.load(f, Loader=yaml.SafeLoader)

        try:
            level = getattr(logging, self.config.get('loglevel', 'WARNING'))
            logging.getLogger().setLevel(level)
        except AttributeError:
            LOG.error('Unknown loglevel %r', self.config['loglevel'])

        self.proxies = []
        for i, rig in enumerate(self.config['rigs']):
            for j, proxy in enumerate(rig['proxies']):
                p = RigProxy(self.config, i, j)
                p.start()
                self.proxies.append(p)

    def poll(self):
        try:
            while True:
                time.sleep(0.25)
        except Exception:
            pass
        for p in self.proxies:
            p.stop()

    @classmethod
    def main(cls):
        logging.basicConfig(level=logging.DEBUG)
        RigProxies('flrigproxy.yaml').poll()


if platform.system() == 'Windows':
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class WinProxyService(win32serviceutil.ServiceFramework):
        _svc_name_ = 'RigProxyService'
        _svc_display_name_ = 'Rig Proxy Service'
        _svc_description_ = 'Rig Proxy Service'

        def __init__(self, args):
            super().__init__(args)
            self.event = win32event.CreateEvent(None, 0, 0, None)

        def SvcDoRun(self):
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            logging.basicConfig(level=logging.DEBUG)
            config_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                'flrigproxy.yaml')
            self.proxies = RigProxies(config_file)
            while True:
                result = win32event.WaitForSingleObject(self.event, 1)
                if result == win32event.WAIT_OBJECT_0:
                    LOG.info('Exiting')
                    break
            for p in self.proxies:
                p.stop()

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
    proxycls = WinProxyService
else:
    proxycls = RigProxies

if __name__ == '__main__':
    proxycls.main()
