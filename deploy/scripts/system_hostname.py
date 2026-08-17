#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import re
import socket
import subprocess
from typing import Any, Callable


class SystemHostnameError(RuntimeError):
    pass


HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
CommandRunner = Callable[[list[str]], dict[str, Any]]


def normalize_hostname(value: Any) -> str:
    return str(value or "").strip().lower().rstrip(".")


def normalize_appliance_hostname(value: str) -> str:
    raw = normalize_hostname(value)
    if not raw:
        raise SystemHostnameError("B1_APPLIANCE_HOSTNAME must not be empty")
    if "://" in raw or "/" in raw or ":" in raw:
        raise SystemHostnameError("B1_APPLIANCE_HOSTNAME must be a hostname/FQDN without scheme, path, or port")
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        pass
    else:
        raise SystemHostnameError("B1_APPLIANCE_HOSTNAME must be a system hostname/FQDN, not an IP address")
    labels = raw.split(".")
    if not all(HOST_LABEL_RE.fullmatch(label) for label in labels):
        raise SystemHostnameError("B1_APPLIANCE_HOSTNAME contains invalid hostname labels")
    return raw


def desired_static_hostname(appliance_hostname: str) -> str:
    return normalize_appliance_hostname(appliance_hostname).split(".", 1)[0]


def default_command_runner(command: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return {"returncode": 127, "stdout": "", "stderr": f"{command[0]} not found"}
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def read_hostnamectl_static(command_runner: CommandRunner = default_command_runner) -> str:
    result = command_runner(["hostnamectl", "--static"])
    if int(result.get("returncode") or 0) != 0:
        return ""
    return normalize_hostname(result.get("stdout"))


def observed_system_hostnames(
    *,
    hostnamectl_static: str | None = None,
    socket_hostname: str | None = None,
    socket_fqdn: str | None = None,
    platform_node: str | None = None,
    command_runner: CommandRunner = default_command_runner,
) -> dict[str, str]:
    static = hostnamectl_static
    if static is None:
        static = read_hostnamectl_static(command_runner)
    return {
        "hostnamectl_static": normalize_hostname(static),
        "socket_hostname": normalize_hostname(socket_hostname if socket_hostname is not None else socket.gethostname()),
        "socket_fqdn": normalize_hostname(socket_fqdn if socket_fqdn is not None else socket.getfqdn()),
        "platform_node": normalize_hostname(platform_node if platform_node is not None else platform.node()),
    }


def hostname_status(
    appliance_hostname: str,
    *,
    hostnamectl_static: str | None = None,
    socket_hostname: str | None = None,
    socket_fqdn: str | None = None,
    platform_node: str | None = None,
    command_runner: CommandRunner = default_command_runner,
) -> dict[str, Any]:
    expected_fqdn = normalize_appliance_hostname(appliance_hostname)
    expected_short = desired_static_hostname(expected_fqdn)
    acceptable = {expected_fqdn, expected_short}
    observed = observed_system_hostnames(
        hostnamectl_static=hostnamectl_static,
        socket_hostname=socket_hostname,
        socket_fqdn=socket_fqdn,
        platform_node=platform_node,
        command_runner=command_runner,
    )
    matching_sources = sorted(source for source, value in observed.items() if value in acceptable)
    apply_command = ["hostnamectl", "set-hostname", expected_short]
    return {
        "format": "b1-ai-hub-system-hostname/v1",
        "hostname_authority": "b1-appliance-config",
        "hostname_source": "system-hostname",
        "network_property_source": "host-dhcp-client",
        "b1_manages_host_networking": False,
        "b1_static_ip_configures": False,
        "network_changes": [],
        "appliance_hostname": expected_fqdn,
        "desired_static_hostname": expected_short,
        "acceptable_system_hostnames": sorted(acceptable),
        "observed": observed,
        "matches": bool(matching_sources),
        "matching_sources": matching_sources,
        "apply_command": apply_command,
        "operator_note": (
            "B1 AI Hub sets only the system hostname. IP address, gateway, routes, "
            "and resolver settings remain owned by the host DHCP client or DHCP reservation."
        ),
    }


def apply_system_hostname(
    appliance_hostname: str,
    *,
    command_runner: CommandRunner = default_command_runner,
) -> dict[str, Any]:
    before = hostname_status(appliance_hostname, command_runner=command_runner)
    if before["matches"]:
        return {**before, "applied": False, "changed": False}

    result = command_runner(before["apply_command"])
    if int(result.get("returncode") or 0) != 0:
        stderr = str(result.get("stderr") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemHostnameError(f"failed to set system hostname with hostnamectl{detail}")

    after = hostname_status(appliance_hostname, command_runner=command_runner)
    return {**after, "applied": True, "changed": True}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify or apply the B1-defined system hostname without changing DHCP-owned networking."
    )
    parser.add_argument(
        "--appliance-hostname",
        default=os.getenv("B1_APPLIANCE_HOSTNAME") or os.getenv("B1_EXPECTED_TARGET_HOST") or os.getenv("B1_HOST_CHAT"),
        help="B1-defined appliance hostname/FQDN. Defaults to B1_APPLIANCE_HOSTNAME.",
    )
    parser.add_argument("--check", action="store_true", help="Exit nonzero when the current system hostname does not match.")
    parser.add_argument("--apply", action="store_true", help="Run hostnamectl set-hostname for the derived static hostname.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    if not args.appliance_hostname:
        raise SystemExit("B1_APPLIANCE_HOSTNAME or --appliance-hostname is required")

    try:
        result = apply_system_hostname(args.appliance_hostname) if args.apply else hostname_status(args.appliance_hostname)
    except SystemHostnameError as exc:
        raise SystemExit(str(exc)) from exc

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        state = "matches" if result["matches"] else "does not match"
        print(
            f"system hostname {state} B1_APPLIANCE_HOSTNAME={result['appliance_hostname']} "
            f"(desired static hostname: {result['desired_static_hostname']})"
        )
        print("networking unchanged: IP/gateway/routes/resolvers remain DHCP-owned")
        if not result["matches"]:
            print("apply with: " + " ".join(result["apply_command"]))

    if args.check and not result["matches"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
