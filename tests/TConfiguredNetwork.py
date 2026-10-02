"""Configured LAN trust, bounded DNS and a total HTTP read budget."""
import socket
from threading import Event, Thread
from time import monotonic
from unittest import TestCase
from unittest.mock import patch

from backend.base.download_job import DownloadErrorCode, DownloadFailure
from backend.implementations.configured_network import bounded_socket
from backend.implementations.download_transport import DownloadHTTP


class ConfiguredNetworkTests(TestCase):
    def test_dns_failure_is_safe(self):
        with patch('socket.getaddrinfo',side_effect=OSError('private resolver detail')):
            with self.assertRaisesRegex(OSError,'Configured service DNS unavailable'):
                bounded_socket(('configured-host',80),.1)

    def test_dns_wait_is_bounded(self):
        release = Event()
        def resolver(*args,**kwargs):
            release.wait(1)
            return []
        try:
            with patch('socket.getaddrinfo',side_effect=resolver):
                start = monotonic()
                with self.assertRaises(TimeoutError): bounded_socket(('configured-host',80),.05)
                self.assertLess(monotonic()-start,.5)
        finally:
            release.set()

    def test_slow_header_cannot_extend_read_budget(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1',0)); listener.listen(1)
        stop = Event()
        def serve():
            connection,_ = listener.accept()
            with connection:
                connection.recv(8192)
                try:
                    connection.sendall(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                    while not stop.wait(.02): connection.sendall(b'a')
                except OSError:
                    pass
        thread = Thread(target=serve,daemon=True); thread.start()
        try:
            start = monotonic()
            with self.assertRaises(DownloadFailure) as error:
                DownloadHTTP(connect_timeout=.2,read_timeout=.15).request('http://127.0.0.1:' + str(listener.getsockname()[1]))
            self.assertEqual(error.exception.code,DownloadErrorCode.TIMEOUT)
            self.assertLess(monotonic()-start,1.)
        finally:
            stop.set(); listener.close(); thread.join(1)
