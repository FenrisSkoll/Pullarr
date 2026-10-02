"""Bounded DNS/connect for trusted administrator-configured LAN services.

Unlike public feed fetching, private addresses are valid here. No environment
proxy, netrc, credential forwarding or redirect policy is added by this helper.
"""
import socket
from queue import Empty, Queue
from threading import BoundedSemaphore, Event, Thread, Timer
from time import monotonic

_DNS_SLOTS = BoundedSemaphore(4)


class ReadDeadline:
    """Interrupt even a trickling HTTP header, not just the response body."""
    def __init__(self, connection, seconds):
        self.expired = Event()
        def interrupt():
            self.expired.set()
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.timer = Timer(seconds, interrupt)
        self.timer.daemon = True
        self.timer.start()

    def cancel(self):
        self.timer.cancel()


def bounded_socket(address, timeout=5, source_address=None, **kwargs):
    deadline = monotonic() + float(timeout)
    if not _DNS_SLOTS.acquire(blocking=False):
        raise TimeoutError('Configured service DNS busy')
    result = Queue(maxsize=1)

    def resolve():
        try:
            result.put((True, socket.getaddrinfo(address[0], address[1], type=socket.SOCK_STREAM)))
        except OSError:
            result.put((False, None))
        finally:
            _DNS_SLOTS.release()

    Thread(target=resolve, name='configured-service-dns', daemon=True).start()
    try:
        valid, values = result.get(timeout=max(.001, deadline - monotonic()))
    except Empty:
        raise TimeoutError('Configured service DNS timeout') from None
    if not valid or not values or len(values) > 16:
        raise OSError('Configured service DNS unavailable')
    for family, kind, protocol, _, destination in values:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError('Configured service connection timeout')
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(remaining)
            if source_address:
                connection.bind(source_address)
            connection.connect(destination)
            return connection
        except OSError:
            connection.close()
    raise OSError('Configured service connection unavailable')
