"""Linux metrics. Collector.sample() is stateful; call from one sampling thread."""
import math
import os
from pathlib import Path
import time


def cpu_counters(text):
    fields = next(line.split()[1:] for line in text.splitlines()
                  if line.startswith('cpu '))
    if len(fields) < 4:
        raise ValueError('Incomplete CPU counters')
    # guest/guest_nice are already included in user/nice.
    values = tuple(map(int, fields[:8]))
    values += (0,) * (8 - len(values))
    if min(values) < 0:
        raise ValueError('Negative CPU counter')
    return values


def cpu_usage(previous, current):
    if previous is None or current is None:
        return None
    delta = [b - a for a, b in zip(previous, current)]
    total = sum(delta)
    if min(delta) < 0 or total <= 0:
        return None
    return 100 * (total - delta[3] - delta[4]) / total


def memory(text):
    values = {}
    for line in text.splitlines():
        key, _, value = line.partition(':')
        if key in ('MemTotal', 'MemAvailable'):
            fields = value.split()
            if len(fields) != 2 or fields[1] != 'kB':
                raise ValueError('Invalid memory units')
            values[key] = int(fields[0]) * 1024
    total, available = values['MemTotal'], values['MemAvailable']
    if not 0 <= available <= total or total <= 0:
        raise ValueError('Invalid memory counters')
    used = total - available
    return dict(total_bytes=total, available_bytes=available,
                used_bytes=used, usage_percent=100 * used / total)


def disk(stats):
    total = stats.f_blocks * stats.f_frsize
    free = stats.f_bfree * stats.f_frsize
    available = stats.f_bavail * stats.f_frsize
    if total <= 0 or not 0 <= available <= free <= total:
        raise ValueError('Invalid filesystem counters')
    used = total - free
    return dict(path='/', total_bytes=total, free_bytes=free,
                available_bytes=available, used_bytes=used,
                usage_percent=100 * used / total)


def default_interface(ipv4, ipv6):
    """Prefer the unique lowest-metric IPv4 default, then IPv6 default.

    Equal-priority routes through different interfaces are ambiguous.
    Missing route tables are allowed, malformed tables fail conservatively.
    """
    candidates = []
    for line in (ipv4 or '').splitlines()[1:]:
        f = line.split()
        if len(f) < 8:
            raise ValueError('Invalid IPv4 route')
        flags = int(f[3], 16)
        if (f[1] == '00000000' and f[7] == '00000000'
                and flags & 1 and not flags & 0x200 and f[0] != 'lo'):
            candidates.append((int(f[6]), f[0]))
    if not candidates:
        for line in (ipv6 or '').splitlines():
            f = line.split()
            if len(f) != 10:
                raise ValueError('Invalid IPv6 route')
            flags = int(f[8], 16)
            if (f[0] == '0' * 32 and f[1] == '00'
                    and f[2] == '0' * 32 and f[3] == '00'
                    and flags & 1 and not flags & 0x200 and f[9] != 'lo'):
                candidates.append((int(f[5], 16), f[9]))
    if not candidates:
        return None
    best = min(metric for metric, _ in candidates)
    interfaces = {name for metric, name in candidates if metric == best}
    return interfaces.pop() if len(interfaces) == 1 else None


def network_counters(text, interface):
    for line in text.splitlines():
        name, sep, rest = line.partition(':')
        if sep and name.strip() == interface:
            f = rest.split()
            if len(f) != 16:
                raise ValueError('Invalid network counters')
            rx, tx = int(f[0]), int(f[8])
            if min(rx, tx) < 0:
                raise ValueError('Negative network counter')
            return rx, tx
    return None


def network_rates(previous, current):
    if previous is None or current is None:
        return None, None
    old_interface, old_time, old_rx, old_tx = previous
    interface, now, rx, tx = current
    elapsed = now - old_time
    if (interface != old_interface or not math.isfinite(elapsed)
            or elapsed <= 0 or rx < old_rx or tx < old_tx):
        return None, None
    return (rx - old_rx) / elapsed, (tx - old_tx) / elapsed


def established(text):
    count = 0
    lines = text.splitlines()
    if not lines or 'local_address' not in lines[0]:
        raise ValueError('Invalid TCP table')
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 4:
            raise ValueError('Invalid TCP entry')
        count += int(fields[3] == '01')
    return count


def optional(function, *args):
    """Expected missing, unreadable or malformed Linux data is unavailable."""
    try:
        return function(*args)
    except (OSError, ValueError, KeyError, IndexError, StopIteration):
        return None


class Collector:
    def __init__(self, proc=Path('/proc'), statvfs=os.statvfs,
                 monotonic=time.monotonic):
        self.proc = Path(proc)
        self.statvfs = statvfs
        self.monotonic = monotonic
        self.previous_cpu = None
        self.previous_network = None

    def read(self, relative):
        return (self.proc / relative).read_text(encoding='ascii')

    def tcp_count(self, version):
        try:
            return established(self.read(f'net/tcp{version}'))
        except FileNotFoundError:
            if version != '6':
                raise
            # A kernel without IPv6 has no tcp6 table. Only count it as zero
            # when the readable protocol registry confirms TCPv6 is absent.
            lines = self.read('net/protocols').splitlines()
            protocols = {line.split()[0] for line in lines[1:] if line.strip()}
            if (lines and lines[0].split()[0] == 'protocol'
                    and 'TCP' in protocols and 'TCPv6' not in protocols):
                return 0
            raise

    def sample(self):
        cpu = optional(lambda: cpu_counters(self.read('stat')))
        usage = cpu_usage(self.previous_cpu, cpu)
        self.previous_cpu = cpu
        ram = optional(lambda: memory(self.read('meminfo')))
        storage = optional(lambda: disk(self.statvfs('/')))
        interface = optional(default_interface,
                             optional(self.read, 'net/route'),
                             optional(self.read, 'net/ipv6_route'))
        counters = optional(lambda: network_counters(self.read('net/dev'), interface))
        current = ((interface, self.monotonic(), *counters)
                   if interface is not None and counters is not None else None)
        rx_rate, tx_rate = network_rates(self.previous_network, current)
        self.previous_network = current
        tcp4 = optional(self.tcp_count, '')
        tcp6 = optional(self.tcp_count, '6')
        return {
            'sampled_at_unix_seconds': time.time(),
            'cpu': {'usage_percent': usage},
            'ram': ram or dict.fromkeys(('total_bytes', 'available_bytes',
                                        'used_bytes', 'usage_percent')),
            'disk': storage or dict(path='/', **dict.fromkeys((
                'total_bytes', 'free_bytes', 'available_bytes',
                'used_bytes', 'usage_percent'))),
            'network': {
                'interface': interface,
                'rx_bytes': counters[0] if current else None,
                'tx_bytes': counters[1] if current else None,
                'rx_bytes_per_second': rx_rate,
                'tx_bytes_per_second': tx_rate,
                'tcp_established_ipv4': tcp4,
                'tcp_established_ipv6': tcp6,
                'tcp_established_total': (tcp4 + tcp6 if tcp4 is not None
                                          and tcp6 is not None else None),
            },
        }
