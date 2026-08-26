#!/usr/bin/env python3
"""Network-namespace launcher using an already-running VPN/TUN transport."""
from __future__ import annotations

import argparse
import configparser
import ipaddress
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

NS = "agy-net"
HOST_IF = "agy-host0"
NET_IF = "agy-net0"
DEFAULT_TRANSPORT_IF = "amn0"
HOST_CIDR = "10.200.200.1/30"
NET_CIDR = "10.200.200.2/30"
HOST_IP = "10.200.200.1"
CONF = Path("/etc/agy-net/awg0.conf")
RUNTIME = Path("/run/agy-net")
OWNERSHIP = RUNTIME / "managed"
TRANSPORT_FILE = RUNTIME / "transport"
MODE_FILE = RUNTIME / "tunnel-mode"
RESOLV_CONF = RUNTIME / "resolv.conf"
DNSMASQ_CONF = RUNTIME / "dnsmasq.conf"
DNSMASQ_PID = RUNTIME / "dnsmasq.pid"
AWG_IF = "awg0"
AWG_RUNTIME_CONF = RUNTIME / f"{AWG_IF}.conf"
LOG_DIR = Path("/var/log/agy-net")
LOG_FILE = LOG_DIR / "agy-net.log"
CONFIG_DIR = Path("/etc/agy-net")
PROFILES_DIR = CONFIG_DIR / "profiles"
DESKTOP_CONFIG = CONFIG_DIR / "desktop.json"
TABLE = "agy_net"
CHECK_URL = "https://api.ipify.org"
TUNNEL_MODE_REUSE = "reuse"
TUNNEL_MODE_AWG = "awg"


class AgyError(RuntimeError):
    pass


def redact(value: str) -> str:
    value = re.sub(r"(?i)(privatekey|presharedkey|authorization|cookie|password|token)\s*=\s*[^\s]+", r"\1=<redacted>", value)
    return re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", value)


def log_event(message: str) -> None:
    if os.geteuid() != 0:
        return
    LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = LOG_DIR.lstat()
    if LOG_DIR.is_symlink() or not LOG_DIR.is_dir() or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise AgyError(f"unsafe log directory: {LOG_DIR}")
    descriptor = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, (redact(message).strip() + "\n").encode("utf-8"))
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


class Runner:
    def __init__(self, dry_run: bool = False, debug: bool = False) -> None:
        self.dry_run, self.debug = dry_run, debug

    def run(self, args: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
        if self.dry_run:
            print("PLAN " + " ".join(args))
            return subprocess.CompletedProcess(args, 0, "", "")
        if self.debug:
            print("DEBUG " + " ".join(args), file=sys.stderr)
        try:
            return subprocess.run(args, check=check, text=True, capture_output=capture)
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "command failed").strip()
            raise AgyError(f"{' '.join(args[:4])}: {message}") from exc


def require_root() -> None:
    if os.geteuid() != 0:
        raise AgyError("network setup requires root; use: sudo agy-net start")


def require_tools(*names: str) -> None:
    missing = [name for name in names if not shutil.which(name)]
    if missing:
        raise AgyError("missing required command(s): " + ", ".join(missing) + ". Install iproute2 and nftables")


def require_forwarding(r: Runner) -> None:
    if r.dry_run:
        print("PLAN verify net.ipv4.ip_forward = 1 (read-only)")
        return
    result = subprocess.run(["sysctl", "-n", "net.ipv4.ip_forward"], check=False, text=True, capture_output=True)
    if result.returncode != 0 or result.stdout.strip() != "1":
        raise AgyError("net.ipv4.ip_forward is not 1; refusing to change host networking. Enable it separately, then retry")


def iface_exists(r: Runner, name: str, namespace: str | None = None) -> bool:
    args = ["ip", "link", "show", "dev", name]
    if namespace:
        args = ["ip", "netns", "exec", namespace, "ip", "link", "show", "dev", name]
    return r.run(args, check=False, capture=True).returncode == 0


def ns_exists(r: Runner) -> bool:
    return r.run(["ip", "netns", "list"], capture=True).stdout.splitlines() and any(
        line.split()[0] == NS for line in r.run(["ip", "netns", "list"], capture=True).stdout.splitlines()
    )


def validate_conf(path: Path) -> tuple[str, int]:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError as exc:
        raise AgyError(f"AmneziaWG config not found: {path}") from exc
    if mode & 0o077:
        raise AgyError(f"refusing insecure config mode {mode:04o}; run: sudo chmod 600 {path}")
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(path)
    if not parser.has_section("Interface") or not parser.has_section("Peer"):
        raise AgyError("awg0.conf must contain [Interface] and [Peer]")
    interface, peer = parser["Interface"], parser["Peer"]
    for section, key in ((interface, "PrivateKey"), (interface, "Address"), (peer, "PublicKey"), (peer, "Endpoint"), (peer, "AllowedIPs")):
        if not section.get(key, "").strip():
            raise AgyError(f"awg0.conf missing {key}")
    endpoint = peer["Endpoint"].strip()
    host, separator, port = endpoint.rpartition(":")
    if not separator or not host or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise AgyError("Phase 1 requires Endpoint as a numeric IPv4 address and port, e.g. 198.51.100.7:51820")
    try:
        ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError as exc:
        raise AgyError("Phase 1 does not resolve endpoint hostnames; use a numeric IPv4 Endpoint") from exc
    return host, int(port)


def interface_value(path: Path, key: str) -> str | None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(path)
    return parser["Interface"].get(key, "").strip() or None


def dns_servers(path: Path) -> list[str]:
    raw = interface_value(path, "DNS")
    if raw is None:
        raise AgyError("AmneziaWG config must provide DNS for namespace isolation")
    servers = [item for item in (part.strip() for part in raw.replace(",", " ").split()) if item]
    if not servers:
        raise AgyError("DNS is empty")
    try:
        for server in servers:
            ipaddress.ip_address(server)
    except ValueError as exc:
        raise AgyError("DNS must contain only IP addresses") from exc
    return servers


def validate_dns_servers(servers: list[str]) -> list[str]:
    if not servers:
        raise AgyError("DNS server list is empty")
    try:
        for server in servers:
            ipaddress.ip_address(server)
    except ValueError as exc:
        raise AgyError("DNS must contain only IP addresses") from exc
    return servers


def parse_dns_override(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    servers = [item for item in (part.strip() for part in raw.replace(",", " ").split()) if item]
    return validate_dns_servers(servers)


def validate_transport_interface(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", name or ""):
        raise AgyError("transport interface must be a Linux interface name of up to 15 letters, digits, ., _ or -")
    return name


def validate_tunnel_mode(value: str) -> str:
    if value not in {TUNNEL_MODE_REUSE, TUNNEL_MODE_AWG}:
        raise AgyError("tunnel mode must be reuse or awg")
    return value


def recorded_transport() -> str:
    try:
        value = TRANSPORT_FILE.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return DEFAULT_TRANSPORT_IF
    except PermissionError as exc:
        raise AgyError("reading the active transport requires root; run: sudo agy-net status") from exc
    return validate_transport_interface(value)


def recorded_tunnel_mode() -> str:
    try:
        value = MODE_FILE.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return TUNNEL_MODE_REUSE
    except PermissionError as exc:
        raise AgyError("reading the active tunnel mode requires root; run: sudo agy-net status") from exc
    return validate_tunnel_mode(value)


def selected_transport(r: Runner, requested: str | None) -> str:
    if requested:
        return validate_transport_interface(requested)
    return recorded_transport() if ns_exists(r) else DEFAULT_TRANSPORT_IF


def selected_tunnel_mode(r: Runner, requested: str | None) -> str:
    if requested:
        return validate_tunnel_mode(requested)
    return recorded_tunnel_mode() if ns_exists(r) else TUNNEL_MODE_REUSE


def dns_settings(config: Path) -> tuple[str, list[str], dict[str, list[str]]]:
    fallback = dns_servers(config)
    source = config.parent / "dns.yaml"
    if not source.exists():
        return "vpn", fallback, {}
    if source.is_symlink() or not source.is_file():
        raise AgyError(f"DNS config must be a regular file: {source}")
    mode, defaults, routes = "vpn", [], {}
    section, route = "", None
    for number, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        text = line.strip()
        if indent == 0 and text == "dns:":
            continue
        if indent == 2 and text.startswith("mode:"):
            mode = text.partition(":")[2].strip()
            continue
        if indent == 2 and text == "default:":
            section, route = "default", None
            continue
        if indent == 2 and text == "routes:":
            section, route = "routes", None
            continue
        if section == "default" and indent == 4 and text.startswith("- "):
            defaults.append(text[2:].strip())
            continue
        if section == "routes" and indent == 4 and text.endswith(":"):
            route = text[:-1].strip().rstrip(".").lower()
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", route or ""):
                raise AgyError(f"invalid split-DNS domain on line {number}")
            routes[route] = []
            continue
        if section == "routes" and route and indent == 6 and text.startswith("- "):
            routes[route].append(text[2:].strip())
            continue
        raise AgyError(f"unsupported dns.yaml syntax on line {number}")
    if mode not in {"vpn", "split"}:
        raise AgyError("dns.mode must be vpn or split")
    defaults = validate_dns_servers(defaults or fallback)
    for domain, servers in routes.items():
        routes[domain] = validate_dns_servers(servers)
    if mode == "split" and not routes:
        raise AgyError("dns.mode split requires at least one routed domain")
    return mode, defaults, routes


def prepare_runtime_dir() -> None:
    if RUNTIME.exists():
        info = RUNTIME.lstat()
        if not RUNTIME.is_dir() or RUNTIME.is_symlink() or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
            raise AgyError(f"unsafe runtime directory: {RUNTIME}")
        return
    RUNTIME.mkdir(mode=0o700, parents=True)


def write_runtime_file(path: Path, content: str, mode: int = 0o600) -> None:
    prepare_runtime_dir()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        os.write(descriptor, content.encode("ascii"))
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def write_resolv_conf(mode: str, servers: list[str]) -> None:
    nameservers = ["127.0.0.53"] if mode == "split" else servers
    content = "".join(f"nameserver {server}\n" for server in nameservers)
    write_runtime_file(RESOLV_CONF, content, 0o644)


def awg_config_without_dns(path: Path) -> str:
    """Keep the AWG config private while preventing awg-quick from changing host DNS."""
    section = ""
    filtered: list[str] = []
    for raw in path.read_text(encoding="ascii").splitlines(keepends=True):
        value = raw.split("#", 1)[0].strip()
        if value.startswith("[") and value.endswith("]"):
            section = value[1:-1].strip().lower()
        if section == "interface" and re.match(r"\s*DNS\s*=", raw, re.IGNORECASE):
            continue
        filtered.append(raw)
    return "".join(filtered)


def prepare_awg_runtime_config(path: Path) -> None:
    write_runtime_file(AWG_RUNTIME_CONF, awg_config_without_dns(path), 0o600)


def start_awg(r: Runner, config: Path) -> None:
    if r.dry_run:
        print(f"PLAN write private {AWG_RUNTIME_CONF} without DNS, mode 0600")
        print(f"PLAN ip netns exec {NS} awg-quick up {AWG_RUNTIME_CONF}")
        return
    prepare_awg_runtime_config(config)
    r.run(["ip", "netns", "exec", NS, "awg-quick", "up", str(AWG_RUNTIME_CONF)])


def stop_awg(r: Runner) -> None:
    if not iface_exists(r, AWG_IF, NS):
        return
    if r.dry_run:
        print(f"PLAN ip netns exec {NS} awg-quick down {AWG_RUNTIME_CONF}")
        return
    r.run(["ip", "netns", "exec", NS, "awg-quick", "down", str(AWG_RUNTIME_CONF)], check=False, capture=True)


def start_dnsmasq(defaults: list[str], routes: dict[str, list[str]]) -> None:
    if not shutil.which("dnsmasq"):
        raise AgyError("split DNS requires dnsmasq; install package dnsmasq")
    lines = [
        "no-resolv",
        "no-hosts",
        "listen-address=127.0.0.53",
        "bind-interfaces",
        "cache-size=1000",
    ]
    lines.extend(f"server={server}" for server in defaults)
    for domain, servers in routes.items():
        lines.extend(f"server=/{domain}/{server}" for server in servers)
    write_runtime_file(DNSMASQ_CONF, "\n".join(lines) + "\n")
    process = subprocess.Popen(
        ["ip", "netns", "exec", NS, "dnsmasq", "--no-daemon", f"--conf-file={DNSMASQ_CONF}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.15)
    if process.poll() is not None:
        detail = (process.stderr.read() if process.stderr else "").strip()
        raise AgyError(f"dnsmasq failed to start: {redact(detail)}")
    write_runtime_file(DNSMASQ_PID, f"{process.pid}\n")


def stop_dnsmasq() -> None:
    try:
        pid = int(DNSMASQ_PID.read_text(encoding="ascii").strip())
    except (FileNotFoundError, ValueError):
        pid = None
    if pid is not None:
        members = subprocess.run(["ip", "netns", "pids", NS], check=False, text=True, capture_output=True).stdout.split()
        if str(pid) in members:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    DNSMASQ_PID.unlink(missing_ok=True)
    DNSMASQ_CONF.unlink(missing_ok=True)


def nft_script(endpoint: str | None = None, port: int | None = None) -> str:
    if endpoint is not None and port is not None:
        permitted_egress = f"""  oifname \"{AWG_IF}\" accept
  oifname \"{NET_IF}\" ip daddr {endpoint} udp dport {port} accept"""
    else:
        permitted_egress = f"  oifname \"{NET_IF}\" accept"
    return f"""table inet {TABLE} {{
 chain output {{
  type filter hook output priority filter; policy drop;
  oifname \"lo\" accept
  ct state established,related accept
{permitted_egress}
 }}
}}\n"""


def host_nft_script(transport: str = DEFAULT_TRANSPORT_IF, endpoint: str | None = None, port: int | None = None) -> str:
    if endpoint is not None and port is not None:
        forward_rule = f"iifname \"{HOST_IF}\" oifname \"{transport}\" ip daddr {endpoint} udp dport {port} accept"
        nat_rule = f"oifname \"{transport}\" ip saddr 10.200.200.0/30 ip daddr {endpoint} udp dport {port} masquerade"
    else:
        forward_rule = f"iifname \"{HOST_IF}\" oifname \"{transport}\" accept"
        nat_rule = f"oifname \"{transport}\" ip saddr 10.200.200.0/30 masquerade"
    return f"""table ip {TABLE} {{
 chain postrouting {{
  type nat hook postrouting priority srcnat; policy accept;
  {nat_rule}
 }}
}}
table inet {TABLE} {{
 chain forward {{
  type filter hook forward priority filter; policy accept;
  {forward_rule}
  iifname \"{HOST_IF}\" drop
  iifname \"{transport}\" oifname \"{HOST_IF}\" ct state established,related accept
 }}
}}\n"""


def apply_nft(r: Runner, args: list[str], rules: str, description: str) -> None:
    if r.dry_run:
        print(f"PLAN {' '.join(args)} <{description}>")
        return
    if r.debug:
        print(f"DEBUG {' '.join(args)} <{description}>", file=sys.stderr)
    process = subprocess.run(args, input=rules, text=True, capture_output=True)
    if process.returncode:
        raise AgyError(process.stderr.strip() or f"failed to install {description}")


def ensure_tables_free(r: Runner) -> None:
    for family in ("ip", "inet"):
        if r.dry_run:
            print(f"PLAN nft list table {family} {TABLE} (read-only ownership check)")
            continue
        result = subprocess.run(["nft", "list", "table", family, TABLE], check=False, text=True, capture_output=True)
        if result.returncode == 0:
            raise AgyError(f"nft table {family} {TABLE} already exists; refusing to touch it")


def mark_owned(r: Runner) -> None:
    if not r.dry_run:
        write_runtime_file(OWNERSHIP, "agy-net\n")


def start(
    r: Runner,
    config: Path,
    transport: str,
    tunnel_mode: str,
    dns_override: list[str] | None = None,
) -> None:
    if not r.dry_run:
        require_tools("ip", "nft")
        if tunnel_mode == TUNNEL_MODE_AWG:
            require_tools("awg-quick")
    if tunnel_mode == TUNNEL_MODE_AWG:
        if dns_override is not None:
            raise AgyError("--dns is only for reuse mode; AWG DNS comes from awg0.conf")
        endpoint, port = validate_conf(config)
        dns_mode, dns_defaults, dns_routes = dns_settings(config)
    elif dns_override is None:
        endpoint = port = None
        validate_conf(config)
        dns_mode, dns_defaults, dns_routes = dns_settings(config)
    else:
        endpoint = port = None
        dns_mode, dns_defaults, dns_routes = "vpn", dns_override, {}
    if not iface_exists(r, transport):
        raise AgyError(f"transport interface {transport} is not up; refusing to touch any tunnel")
    require_forwarding(r)
    if ns_exists(r):
        active_transport = recorded_transport()
        active_mode = recorded_tunnel_mode()
        if active_transport != transport or active_mode != tunnel_mode:
            raise AgyError(f"{NS} is already running over {active_transport} in {active_mode} mode; stop it before changing transport or mode")
        print(f"{NS} is already running over {transport} in {tunnel_mode} mode")
        return
    ensure_tables_free(r)
    try:
        r.run(["ip", "netns", "add", NS])
        r.run(["ip", "link", "add", HOST_IF, "type", "veth", "peer", "name", NET_IF])
        r.run(["ip", "link", "set", NET_IF, "netns", NS])
        r.run(["ip", "addr", "add", HOST_CIDR, "dev", HOST_IF])
        r.run(["ip", "link", "set", HOST_IF, "up"])
        r.run(["ip", "netns", "exec", NS, "ip", "link", "set", "lo", "up"])
        r.run(["ip", "netns", "exec", NS, "ip", "addr", "add", NET_CIDR, "dev", NET_IF])
        r.run(["ip", "netns", "exec", NS, "ip", "link", "set", NET_IF, "up"])
        r.run(["ip", "netns", "exec", NS, "ip", "route", "replace", "default", "via", HOST_IP, "dev", NET_IF])
        # The host transport is only reused for bootstrap; managed AWG stays inside the namespace.
        apply_nft(r, ["nft", "-f", "-"], host_nft_script(transport, endpoint, port), "host NAT rules")
        mark_owned(r)
        if not r.dry_run:
            write_runtime_file(TRANSPORT_FILE, transport + "\n")
            write_runtime_file(MODE_FILE, tunnel_mode + "\n")
        if tunnel_mode == TUNNEL_MODE_AWG:
            start_awg(r, config)
        if r.dry_run:
            print("PLAN write /run/agy-net/resolv.conf from config DNS (mode 0644)")
        else:
            write_resolv_conf(dns_mode, dns_defaults)
        apply_nft(r, ["ip", "netns", "exec", NS, "nft", "-f", "-"], nft_script(endpoint, port), "namespace kill-switch rules")
        if dns_mode == "split":
            if r.dry_run:
                print("PLAN start dnsmasq only inside agy-net on 127.0.0.53")
            else:
                start_dnsmasq(dns_defaults, dns_routes)
        if not r.dry_run:
            log_event(f"started namespace={NS} transport={transport}")
    except Exception:
        if not r.dry_run:
            stop(r, quiet=True)
        raise


def stop(r: Runner, quiet: bool = False) -> None:
    if not r.dry_run:
        stop_dnsmasq()
    stop_awg(r)
    if ns_exists(r):
        r.run(["ip", "netns", "delete", NS], check=False)
    if iface_exists(r, HOST_IF):
        r.run(["ip", "link", "delete", HOST_IF], check=False)
    if r.dry_run or OWNERSHIP.exists():
        r.run(["nft", "delete", "table", "ip", TABLE], check=False)
        r.run(["nft", "delete", "table", "inet", TABLE], check=False)
        if not r.dry_run:
            OWNERSHIP.unlink(missing_ok=True)
            TRANSPORT_FILE.unlink(missing_ok=True)
            MODE_FILE.unlink(missing_ok=True)
            RESOLV_CONF.unlink(missing_ok=True)
            AWG_RUNTIME_CONF.unlink(missing_ok=True)
    if not quiet:
        if not r.dry_run:
            log_event("stopped namespace=agy-net")
        print("agy-net stopped; namespace and agy-net nft table removed")


def status(r: Runner, transport: str, tunnel_mode: str) -> int:
    if not ns_exists(r):
        print("[FAIL] namespace agy-net is absent\nRun: sudo agy-net start")
        return 1
    bad = False
    print("[OK] namespace agy-net")
    for name in (NET_IF,):
        present = iface_exists(r, name, NS)
        bad |= not present
        print(("[OK] " if present else "[FAIL] ") + name)
    if tunnel_mode == TUNNEL_MODE_AWG:
        present = iface_exists(r, AWG_IF, NS)
        bad |= not present
        print(("[OK] " if present else "[FAIL] ") + f"{AWG_IF} (managed AWG)")
    present = iface_exists(r, transport)
    bad |= not present
    print(("[OK] " if present else "[FAIL] ") + f"host transport {transport}")
    print(f"[OK] tunnel mode {tunnel_mode}")
    return 1 if bad else 0


def invoking_user() -> pwd.struct_passwd:
    if os.geteuid() != 0:
        raise AgyError("entering a named network namespace requires root; run with sudo")
    name = os.environ.get("SUDO_USER")
    if not name or name == "root":
        raise AgyError("run agy-net through sudo from a regular desktop user")
    try:
        return pwd.getpwnam(name)
    except KeyError as exc:
        raise AgyError(f"sudo user does not exist: {name}") from exc


def user_environment(account: pwd.struct_passwd) -> dict[str, str]:
    environment = {
        "HOME": account.pw_dir,
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        "SHELL": account.pw_shell or "/bin/sh",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY", "LANG", "LC_ALL"):
        if value := os.environ.get(key):
            environment[key] = value
    return environment


def resolve_launcher_command(command: list[str], account: pwd.struct_passwd) -> list[str]:
    if not command:
        raise AgyError("usage: agy-net run <command> [args...]")
    if command[0] != "antigravity":
        return command
    candidates = (
        "/usr/bin/antigravity",
        "/usr/local/bin/antigravity",
        "/opt/Antigravity/antigravity",
        "/opt/google/antigravity/antigravity",
        str(Path(account.pw_dir) / ".local/bin/antigravity"),
    )
    found = next((path for path in candidates if os.path.isfile(path) and os.access(path, os.X_OK)), None)
    if found is None:
        raise AgyError("Antigravity was not found in the supported locations; pass its absolute path to agy-net run")
    return [found, *command[1:]]


def mount_private_resolver() -> None:
    if not RESOLV_CONF.is_file():
        raise AgyError("namespace resolver is absent; restart agy-net")
    try:
        os.unshare(getattr(os, "CLONE_NEWNS", 0x00020000))
        subprocess.run(["mount", "--make-rprivate", "/"], check=True, capture_output=True, text=True)
        subprocess.run(["mount", "--bind", str(RESOLV_CONF), "/etc/resolv.conf"], check=True, capture_output=True, text=True)
        subprocess.run(["mount", "-o", "remount,bind,ro", "/etc/resolv.conf"], check=True, capture_output=True, text=True)
    except (AttributeError, OSError, subprocess.CalledProcessError) as exc:
        raise AgyError("could not create private resolver mount for application") from exc


def execute_in_namespace(user: str, command: list[str]) -> int:
    if os.geteuid() != 0:
        raise AgyError("internal namespace executor must start as root")
    if not command:
        raise AgyError("internal namespace executor received no command")
    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise AgyError(f"unknown target user: {user}") from exc
    if account.pw_uid == 0:
        raise AgyError("refusing to run application as root")
    environment = user_environment(account)
    mount_private_resolver()
    os.initgroups(account.pw_name, account.pw_gid)
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)
    os.execvpe(command[0], command, environment)
    raise AssertionError("unreachable")


def run_in_namespace(r: Runner, command: list[str]) -> int:
    account = invoking_user()
    if not ns_exists(r):
        raise AgyError("namespace is not running; use sudo agy-net start")
    resolved = resolve_launcher_command(command, account)
    executable = str(Path(sys.argv[0]).resolve())
    return subprocess.run(["ip", "netns", "exec", NS, executable, "_exec", "--user", account.pw_name, *resolved]).returncode


def test_killswitch(r: Runner, tunnel_mode: str) -> int:
    if not ns_exists(r) or not iface_exists(r, NET_IF, NS):
        raise AgyError("start agy-net before test-killswitch")
    if tunnel_mode == TUNNEL_MODE_AWG and not iface_exists(r, AWG_IF, NS):
        raise AgyError("managed AWG interface is absent; restart agy-net")
    print("Testing VPN connectivity...")
    before = run_in_namespace(r, ["curl", "--fail", "--silent", "--show-error", "--max-time", "12", CHECK_URL])
    if tunnel_mode == TUNNEL_MODE_AWG:
        r.run(["ip", "netns", "exec", NS, "ip", "link", "set", "dev", AWG_IF, "down"])
        try:
            blocked = run_in_namespace(r, ["curl", "--fail", "--silent", "--show-error", "--max-time", "5", CHECK_URL]) != 0
        finally:
            r.run(["ip", "netns", "exec", NS, "ip", "link", "set", "dev", AWG_IF, "up"])
    else:
        r.run(["ip", "netns", "exec", NS, "ip", "route", "del", "default"])
        try:
            blocked = run_in_namespace(r, ["curl", "--fail", "--silent", "--show-error", "--max-time", "5", CHECK_URL]) != 0
        finally:
            r.run(["ip", "netns", "exec", NS, "ip", "route", "replace", "default", "via", HOST_IP, "dev", NET_IF])
    if before == 0 and blocked:
        print("[OK] namespace egress is blocked when its only route is removed")
        return 0
    print("[FAIL] kill-switch test failed")
    return 1


def doctor(
    r: Runner,
    config: Path,
    transport: str,
    tunnel_mode: str,
    dns_override: list[str] | None = None,
) -> int:
    bad = False
    for tool in ("ip", "nft", "curl", "systemctl"):
        available = bool(shutil.which(tool)); bad |= not available
        print(f"[{'OK' if available else 'FAIL'}] {tool}" + ("" if available else " — install required package"))
    if tunnel_mode == TUNNEL_MODE_AWG:
        available = bool(shutil.which("awg-quick")); bad |= not available
        print(f"[{'OK' if available else 'FAIL'}] awg-quick" + ("" if available else " — install AmneziaWG tools"))
    if dns_override is None:
        try:
            validate_conf(config); print("[OK] AmneziaWG config permissions and shape (not applied to host)")
        except AgyError as exc:
            bad = True; print(f"[FAIL] AmneziaWG config — {exc}")
        try:
            mode, _, _ = dns_settings(config)
            print(f"[OK] DNS mode: {mode}")
            if mode == "split" and not shutil.which("dnsmasq"):
                bad = True; print("[FAIL] dnsmasq — install dnsmasq for split DNS")
        except AgyError as exc:
            bad = True; print(f"[FAIL] DNS config — {exc}")
    else:
        print("[OK] explicit namespace DNS")
    forwarding = subprocess.run(["sysctl", "-n", "net.ipv4.ip_forward"], check=False, text=True, capture_output=True)
    if forwarding.returncode == 0 and forwarding.stdout.strip() == "1":
        print("[OK] IPv4 forwarding")
    else:
        bad = True; print("[FAIL] IPv4 forwarding — enable net.ipv4.ip_forward=1")
    bad |= status(r, transport, tunnel_mode) != 0
    return 1 if bad else 0


def namespace_ip(arguments: list[str]) -> int:
    require_root()
    if not ns_exists(Runner()):
        raise AgyError("namespace is not running; use sudo agy-net start")
    return subprocess.run(["ip", "netns", "exec", NS, "ip", *arguments]).returncode


def dns_test(r: Runner, hostname: str) -> int:
    if not hostname or "/" in hostname or len(hostname) > 253:
        raise AgyError("dns-test expects a hostname")
    print(f"Resolving {hostname} inside {NS}...")
    return run_in_namespace(r, ["getent", "ahostsv4", hostname])


def transport_status(transport: str, tunnel_mode: str) -> int:
    require_root()
    if tunnel_mode == TUNNEL_MODE_AWG:
        if not iface_exists(Runner(), AWG_IF, NS):
            raise AgyError(f"managed {AWG_IF} interface is not up")
        result = subprocess.run(
            ["ip", "netns", "exec", NS, "awg", "show", AWG_IF, "latest-handshakes"],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode:
            raise AgyError("could not read managed AWG handshake state")
        match = re.search(r"\s(\d+)\s*$", result.stdout)
        if not match or match.group(1) == "0":
            print("[WARN] managed AWG has no completed handshake yet")
            return 1
        age = max(0, int(time.time()) - int(match.group(1)))
        print(f"[OK] managed AWG latest handshake: {age} sec ago")
        return 0
    if not iface_exists(Runner(), transport):
        raise AgyError(f"transport interface {transport} is not up")
    return subprocess.run(["ip", "-details", "link", "show", "dev", transport]).returncode


def connectivity_test(r: Runner) -> int:
    require_root()
    host = subprocess.run(["curl", "--fail", "--silent", "--show-error", "--max-time", "15", CHECK_URL], text=True, capture_output=True)
    if host.returncode:
        print("[FAIL] host connectivity", file=sys.stderr)
        return host.returncode
    print(f"[OK] host public IP: {host.stdout.strip()}")
    print("[INFO] namespace public IP:")
    return run_in_namespace(r, ["curl", "--fail", "--silent", "--show-error", "--max-time", "15", CHECK_URL])


def show_logs() -> int:
    require_root()
    if not LOG_FILE.exists():
        print("No agy-net log entries yet")
        return 0
    print(redact(LOG_FILE.read_text(encoding="utf-8", errors="replace")), end="")
    return 0


def profile_config(profile: str | None, fallback: Path) -> Path:
    if profile is None:
        return fallback
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", profile):
        raise AgyError("profile must contain only letters, digits, _ or -")
    return PROFILES_DIR / profile / "awg0.conf"


def safe_copy_config(source: Path, destination: Path, dry_run: bool = False) -> None:
    if dry_run:
        print(f"PLAN securely copy {source} to {destination} as root:root mode 0600")
        return
    require_root()
    try:
        source_info = source.lstat()
    except FileNotFoundError as exc:
        raise AgyError(f"config source does not exist: {source}") from exc
    if source.is_symlink() or not stat.S_ISREG(source_info.st_mode):
        raise AgyError("config source must be a regular file, not a symlink")
    if stat.S_IMODE(source_info.st_mode) & 0o077:
        raise AgyError("config source must be mode 0600")
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    parent_info = destination.parent.lstat()
    if destination.parent.is_symlink() or not destination.parent.is_dir() or parent_info.st_uid != 0 or stat.S_IMODE(parent_info.st_mode) & 0o022:
        raise AgyError(f"unsafe configuration directory: {destination.parent}")
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    temporary = destination.parent / f".{destination.name}.{os.getpid()}.tmp"
    try:
        destination_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            while chunk := os.read(source_fd, 65536):
                os.write(destination_fd, chunk)
            os.fchmod(destination_fd, 0o600)
            os.fchown(destination_fd, 0, 0)
        finally:
            os.close(destination_fd)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_fd)
    validate_conf(destination)
    log_event(f"installed configuration at {destination}")


def safe_write_root_file(destination: Path, content: str, mode: int) -> None:
    """Atomically write a root-owned configuration file without following links."""
    require_root()
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    parent_info = destination.parent.lstat()
    if destination.parent.is_symlink() or not destination.parent.is_dir() or parent_info.st_uid != 0 or stat.S_IMODE(parent_info.st_mode) & 0o022:
        raise AgyError(f"unsafe configuration directory: {destination.parent}")
    temporary = destination.parent / f".{destination.name}.{os.getpid()}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            os.write(descriptor, content.encode("utf-8"))
            os.fchmod(descriptor, mode)
            os.fchown(descriptor, 0, 0)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def configure_desktop(binary: Path, dry_run: bool = False) -> None:
    """Store only non-secret GUI session data for the system launcher unit."""
    account = invoking_user()
    if not binary.is_absolute():
        raise AgyError("Antigravity binary must be given as an absolute path")
    try:
        info = binary.stat()
    except FileNotFoundError as exc:
        raise AgyError(f"Antigravity binary not found: {binary}") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(binary, os.X_OK):
        raise AgyError(f"Antigravity binary is not executable: {binary}")
    environment = user_environment(account)
    if not environment.get("DISPLAY") and not environment.get("WAYLAND_DISPLAY"):
        raise AgyError("desktop-configure needs DISPLAY or WAYLAND_DISPLAY from the active desktop session")
    # Environment comes from the desktop session, never from an untrusted .desktop argument.
    environment.pop("ELECTRON_RUN_AS_NODE", None)
    content = json.dumps(
        {"user": account.pw_name, "binary": str(binary), "environment": environment},
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    if dry_run:
        print(f"PLAN write non-secret desktop launch metadata to {DESKTOP_CONFIG} as root:root mode 0644")
        return
    safe_write_root_file(DESKTOP_CONFIG, content, 0o644)
    log_event(f"configured desktop launcher user={account.pw_name} binary={binary}")
    print("Desktop launcher metadata installed. The panel/desktop shortcut will request authorization, then start Antigravity inside agy-net.")


def install_from_source() -> None:
    require_root()
    installer = Path(__file__).resolve().parent / "install.sh"
    if not installer.is_file() or installer.is_symlink():
        raise AgyError("install command is available only from an unpacked agy-net source directory; use install.sh")
    subprocess.run(["/bin/sh", str(installer)], check=True)


def uninstall(purge_config: bool, dry_run: bool = False) -> None:
    if dry_run:
        print("PLAN stop agy-net; remove only agy-net systemd units, binary and logs" + ("; purge /etc/agy-net/awg0.conf" if purge_config else "; retain configuration"))
        return
    require_root()
    stop(Runner(), quiet=True)
    for unit in (
        Path("/etc/systemd/system/agy-net.service"),
        Path("/etc/systemd/system/agy-net@.service"),
        Path("/etc/systemd/system/agy-net-antigravity@.service"),
    ):
        unit.unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True, text=True)
    if purge_config:
        CONF.unlink(missing_ok=True)
        DESKTOP_CONFIG.unlink(missing_ok=True)
    LOG_FILE.unlink(missing_ok=True)
    helper = Path("/usr/local/lib/agy-net/desktop-exec")
    helper.unlink(missing_ok=True)
    try:
        helper.parent.rmdir()
    except OSError:
        pass
    binary = Path("/usr/local/bin/agy-net")
    if binary.is_file() and "Network-namespace launcher" in binary.read_text(encoding="utf-8", errors="ignore"):
        binary.unlink()
    print("agy-net uninstalled" + (" and default config purged" if purge_config else "; config retained"))


def main() -> int:
    parser = argparse.ArgumentParser(prog="agy-net")
    parser.add_argument("--config", type=Path, default=CONF)
    parser.add_argument("--profile", help="configuration profile name under /etc/agy-net/profiles")
    parser.add_argument("--transport-interface", default=os.environ.get("AGY_NET_TRANSPORT_INTERFACE"), metavar="IFACE", help="existing VPN/TUN interface (default: amn0)")
    parser.add_argument("--tunnel-mode", default=os.environ.get("AGY_NET_TUNNEL_MODE"), choices=(TUNNEL_MODE_REUSE, TUNNEL_MODE_AWG), help="reuse an existing TUN, or create private awg0 inside agy-net")
    parser.add_argument("--dns", default=os.environ.get("AGY_NET_DNS"), metavar="IP[,IP]", help="namespace DNS override; required for VLESS without an AWG config")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug", action="store_true")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("install", "start", "stop", "restart", "status", "doctor", "test-killswitch", "test", "wg-status", "logs", "uninstall"):
        sub.add_parser(name)
    run_p = sub.add_parser("run"); run_p.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("shell")
    ip_p = sub.add_parser("ip"); ip_p.add_argument("arguments", nargs=argparse.REMAINDER)
    dns_p = sub.add_parser("dns-test"); dns_p.add_argument("hostname", nargs="?", default="api.ipify.org")
    configure_p = sub.add_parser("configure"); configure_p.add_argument("source", type=Path)
    desktop_p = sub.add_parser("desktop-configure")
    desktop_p.add_argument("--binary", type=Path, required=True, help="absolute Antigravity executable path")
    uninstall_p = sub.choices["uninstall"]; uninstall_p.add_argument("--purge-config", action="store_true")
    exec_p = sub.add_parser("_exec", help=argparse.SUPPRESS)
    exec_p.add_argument("--user", required=True)
    exec_p.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(); r = Runner(args.dry_run, args.debug)
    try:
        config = profile_config(args.profile, args.config)
        transport = selected_transport(r, args.transport_interface)
        tunnel_mode = selected_tunnel_mode(r, args.tunnel_mode)
        dns_override = parse_dns_override(args.dns) if args.action in {"start", "restart", "doctor"} else None
        if args.action == "start":
            if not args.dry_run:
                require_root()
            start(r, config, transport, tunnel_mode, dns_override)
        elif args.action == "stop":
            if not args.dry_run:
                require_root()
            stop(r)
        elif args.action == "restart":
            if not args.dry_run:
                require_root()
            stop(r, quiet=True); start(r, config, transport, tunnel_mode, dns_override)
        elif args.action == "status": return status(r, transport, tunnel_mode)
        elif args.action == "doctor": return doctor(r, config, transport, tunnel_mode, dns_override)
        elif args.action == "run": return run_in_namespace(r, args.command)
        elif args.action == "shell": return run_in_namespace(r, ["/bin/bash"])
        elif args.action == "_exec": return execute_in_namespace(args.user, args.command)
        elif args.action == "ip": return namespace_ip(args.arguments)
        elif args.action == "dns-test": return dns_test(r, args.hostname)
        elif args.action == "wg-status": return transport_status(transport, tunnel_mode)
        elif args.action == "test": return connectivity_test(r)
        elif args.action == "logs": return show_logs()
        elif args.action == "configure": safe_copy_config(args.source, config, args.dry_run)
        elif args.action == "desktop-configure": configure_desktop(args.binary, args.dry_run)
        elif args.action == "install":
            if args.dry_run:
                print("PLAN run install.sh from the unpacked agy-net source directory")
            else:
                install_from_source()
        elif args.action == "uninstall": uninstall(args.purge_config, args.dry_run)
        elif args.action == "test-killswitch":
            if not args.dry_run:
                require_root()
            return test_killswitch(r, tunnel_mode)
        return 0
    except AgyError as exc:
        print(f"error: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
