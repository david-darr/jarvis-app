"""Listener and peer checks, independent of Host and forwarded headers."""
import ipaddress


def local_terminal_request(scope):
    def loopback(address):
        try:
            ip = ipaddress.ip_address(address)
            return ip.is_loopback or bool(getattr(ip, 'ipv4_mapped', None) and ip.ipv4_mapped.is_loopback)
        except ValueError:
            return False
    server, client = scope.get('server'), scope.get('client')
    return bool(scope.get('scheme') == 'http' and server and client and loopback(server[0]) and loopback(client[0]))
