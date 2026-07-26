#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


class NetworkDhcpPlanError(RuntimeError):
    pass


CommandRunner = Callable[[list[str]], dict[str, Any]]


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def default_command_runner(command: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return {"returncode": 127, "stdout": "", "stderr": f"{command[0]} not found"}
    except subprocess.TimeoutExpired as exc:
        return {"returncode": 124, "stdout": exc.stdout or "", "stderr": exc.stderr or "command timed out"}
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def parse_nmcli_key_values(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def split_nmcli_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip() and item.strip() != "--"]


def parse_ipv4_address(value: str) -> str:
    raw = value.strip()
    if "/" in raw:
        raw = raw.split("/", 1)[0]
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise NetworkDhcpPlanError(f"invalid IPv4 address: {value}") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise NetworkDhcpPlanError(f"address must be IPv4 for DHCP readiness planning: {value}")
    return str(address)


def parse_ipv4_address_entry(value: str) -> dict[str, Any]:
    raw = value.strip()
    if not raw:
        raise NetworkDhcpPlanError("IPv4 address entries must not be empty")
    if "/" not in raw:
        return {"address": parse_ipv4_address(raw), "prefix_length": None, "cidr": ""}
    try:
        interface = ipaddress.ip_interface(raw)
    except ValueError as exc:
        raise NetworkDhcpPlanError(f"invalid IPv4 address: {value}") from exc
    if not isinstance(interface, ipaddress.IPv4Interface):
        raise NetworkDhcpPlanError(f"address must be IPv4 for DHCP readiness planning: {value}")
    return {
        "address": str(interface.ip),
        "prefix_length": interface.network.prefixlen,
        "cidr": f"{interface.ip}/{interface.network.prefixlen}",
    }


def parse_address_record(value: str, *, default_purpose: str) -> dict[str, Any]:
    raw = value.strip()
    if not raw:
        raise NetworkDhcpPlanError("address entries must not be empty")
    address, sep, purpose = raw.partition("=")
    parsed = parse_ipv4_address_entry(address)
    return {
        "address": parsed["address"],
        "prefix_length": parsed["prefix_length"],
        "cidr": parsed["cidr"],
        "purpose": purpose.strip() or default_purpose,
    }


def parse_required_address(value: str) -> dict[str, Any]:
    return parse_address_record(value, default_purpose="operator-specified")


def parse_static_infrastructure_address(value: str) -> dict[str, Any]:
    return parse_address_record(value, default_purpose="host-network-infrastructure")


def parse_address_records(
    values: list[str],
    env_value: str = "",
    *,
    parser: Callable[[str], dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_items = list(values)
    if env_value.strip():
        raw_items.extend(item for item in env_value.replace(",", " ").split() if item.strip())
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_items:
        record = parser(item)
        if record["address"] in seen:
            continue
        seen.add(record["address"])
        parsed.append(record)
    return parsed


def parse_required_addresses(values: list[str], env_value: str = "") -> list[dict[str, Any]]:
    return parse_address_records(values, env_value, parser=parse_required_address)


def parse_static_infrastructure_addresses(values: list[str], env_value: str = "") -> list[dict[str, Any]]:
    return parse_address_records(values, env_value, parser=parse_static_infrastructure_address)


def dns_admin_summary(url: str) -> dict[str, Any]:
    raw = url.strip()
    if not raw:
        return {"configured": False}
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        return {"configured": False, "url": raw[:500], "warnings": [f"invalid DNS admin URL: {exc}"]}
    warnings: list[str] = []
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        warnings.append("DNS admin URL must be an http(s) URL with a host")
    if parsed.username or parsed.password:
        warnings.append("DNS admin URL must not include credentials")
    if parsed.query or parsed.fragment:
        warnings.append("DNS admin URL must not include query strings or fragments")
    try:
        port = parsed.port
    except ValueError as exc:
        port = None
        warnings.append(f"DNS admin URL has an invalid port: {exc}")
    return {
        "configured": not warnings,
        "url": raw[:500],
        "scheme": parsed.scheme,
        "host": parsed.hostname or "",
        "port": port,
        "warnings": warnings,
    }


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def connection_profile(
    connection: str,
    *,
    command_runner: CommandRunner = default_command_runner,
) -> dict[str, Any]:
    result = command_runner(
        [
            "nmcli",
            "-t",
            "-f",
            "connection.id,connection.uuid,connection.interface-name,ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns,ipv4.dns-search,ipv4.ignore-auto-dns,ipv6.method",
            "con",
            "show",
            connection,
        ]
    )
    if int(result.get("returncode") or 0) != 0:
        stderr = str(result.get("stderr") or "").strip()
        raise NetworkDhcpPlanError(f"could not inspect NetworkManager connection {connection!r}: {stderr or 'nmcli failed'}")
    fields = parse_nmcli_key_values(str(result.get("stdout") or ""))
    interface = fields.get("connection.interface-name", "")
    device: dict[str, Any] = {}
    if interface:
        device_result = command_runner(["nmcli", "-t", "-f", "GENERAL.DEVICE,GENERAL.HWADDR", "dev", "show", interface])
        if int(device_result.get("returncode") or 0) == 0:
            device = parse_nmcli_key_values(str(device_result.get("stdout") or ""))
    address_entries = [parse_ipv4_address_entry(item) for item in split_nmcli_list(fields.get("ipv4.addresses", ""))]
    return {
        "connection": connection,
        "id": fields.get("connection.id", connection),
        "uuid": fields.get("connection.uuid", ""),
        "interface": interface,
        "mac_address": device.get("GENERAL.HWADDR", ""),
        "ipv4_method": fields.get("ipv4.method", ""),
        "ipv4_address_entries": address_entries,
        "ipv4_addresses": [item["address"] for item in address_entries],
        "ipv4_gateway": fields.get("ipv4.gateway", ""),
        "ipv4_dns": [parse_ipv4_address(item) for item in split_nmcli_list(fields.get("ipv4.dns", ""))],
        "ipv4_dns_search": split_nmcli_list(fields.get("ipv4.dns-search", "")),
        "ipv4_ignore_auto_dns": fields.get("ipv4.ignore-auto-dns", ""),
        "ipv6_method": fields.get("ipv6.method", ""),
    }


def candidate_connection_name(connection: str) -> str:
    base = f"{connection}-dhcp-candidate".strip("-")
    return base[:60] or "b1-dhcp-candidate"


def resolve_static_candidate_cidrs(
    profile: dict[str, Any],
    static_infrastructure_addresses: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    current_by_address = {item["address"]: item for item in profile.get("ipv4_address_entries", [])}
    resolved: list[dict[str, Any]] = []
    missing_cidrs: list[str] = []
    for item in static_infrastructure_addresses:
        current = current_by_address.get(item["address"])
        cidr = item.get("cidr") or (current or {}).get("cidr") or ""
        if not cidr:
            missing_cidrs.append(item["address"])
        resolved.append({**item, "cidr": cidr})
    return resolved, missing_cidrs


def migration_commands(connection: str, candidate: str, static_ipv4_cidrs: list[str]) -> dict[str, list[str]]:
    quoted_connection = shell_quote(connection)
    quoted_candidate = shell_quote(candidate)
    if static_ipv4_cidrs:
        quoted_static_addresses = shell_quote(",".join(static_ipv4_cidrs))
        address_clause = f"ipv4.addresses {quoted_static_addresses}"
    else:
        address_clause = "ipv4.addresses ''"
    return {
        "snapshot": [
            f"nmcli connection show {quoted_connection}",
            "ip -4 address show",
            "ip -4 route show default",
            "resolvectl status",
        ],
        "stage_candidate_profile": [
            f"nmcli connection clone {quoted_connection} {quoted_candidate}",
            (
                f"nmcli connection modify {quoted_candidate} "
                f"ipv4.method auto {address_clause} ipv4.gateway '' ipv4.dns '' "
                "ipv4.dns-search '' ipv4.ignore-auto-dns no"
            ),
        ],
        "activate_after_reservations_are_confirmed": [
            f"nmcli connection up {quoted_candidate}",
        ],
        "verify": [
            "ip -4 address show",
            "ip -4 route show default",
            "resolvectl dns",
            "getent ahostsv4 ai.b1.germering control.ai.b1.germering api.ai.b1.germering",
        ],
        "rollback": [
            f"nmcli connection up {quoted_connection}",
            f"nmcli connection delete {quoted_candidate}",
        ],
    }


def build_plan(
    *,
    connection: str,
    appliance_hostname: str,
    dns_admin_url: str,
    required_addresses: list[dict[str, Any]],
    static_infrastructure_addresses: list[dict[str, Any]] | None = None,
    reservation_confirmed: bool = False,
    command_runner: CommandRunner = default_command_runner,
    now: datetime | None = None,
) -> dict[str, Any]:
    profile = connection_profile(connection, command_runner=command_runner)
    static_infrastructure_addresses = static_infrastructure_addresses or []
    required_ipv4 = [item["address"] for item in required_addresses]
    static_ipv4 = [item["address"] for item in static_infrastructure_addresses]
    current_addresses = set(profile["ipv4_addresses"])
    required_set = set(required_ipv4)
    static_set = set(static_ipv4)
    missing_current_addresses = sorted(required_set - current_addresses)
    missing_static_current_addresses = sorted(static_set - current_addresses)
    static_candidate_addresses, missing_static_cidrs = resolve_static_candidate_cidrs(profile, static_infrastructure_addresses)
    candidate = candidate_connection_name(connection)
    warnings: list[str] = []
    blockers: list[str] = []

    if not required_set:
        blockers.append("no DHCP-reserved appliance IPv4 address was supplied")
    if required_set & static_set:
        blockers.append(
            "an IPv4 address cannot be both a DHCP-reserved appliance address and a static host-infrastructure address"
        )
    if profile["ipv4_method"] and profile["ipv4_method"] not in {"auto", "manual"}:
        blockers.append(
            f"NetworkManager connection {connection} uses unsupported ipv4.method={profile['ipv4_method']}; review manually"
        )
    if not reservation_confirmed:
        blockers.append("operator has not confirmed matching DHCP reservations in the LAN DHCP server")
    if len(required_set) > 1:
        blockers.append(
            "multiple required IPv4 addresses were supplied; an ordinary NetworkManager DHCP profile normally proves only one IPv4 lease"
        )
        warnings.append(
            "Choose one DHCP-reserved appliance address and update LAN DNS records; static DNS/DHCP infrastructure addresses belong in static_infrastructure_addresses."
        )
    if missing_static_current_addresses:
        blockers.append(
            "static host-infrastructure address is not present on the current connection: "
            + ", ".join(missing_static_current_addresses)
        )
    if missing_static_cidrs:
        blockers.append(
            "static host-infrastructure address is missing a CIDR prefix and it could not be inferred from the current profile: "
            + ", ".join(missing_static_cidrs)
        )
    if profile["ipv4_method"] == "manual":
        warnings.append(
            "current connection uses manual IPv4; the staged candidate keeps only static infrastructure addresses and gets B1 appliance networking by DHCP"
        )
    if missing_current_addresses:
        warnings.append(
            "current profile does not presently hold all required addresses: " + ", ".join(missing_current_addresses)
        )
    if profile["ipv4_gateway"] and profile["ipv4_method"] == "manual":
        warnings.append("current default gateway is manually configured; the DHCP candidate must receive it from DHCP")
    if profile["ipv4_dns"] and profile["ipv4_ignore_auto_dns"].lower() in {"yes", "true"}:
        warnings.append("current DNS servers are manually configured and auto-DNS is ignored; the DHCP candidate clears this")

    ready_to_apply = not blockers
    status = "ready" if ready_to_apply else "operator-review-required"
    return {
        "format": "b1-ai-hub-network-dhcp-plan/v1",
        "generated_at": (now or utc_now()).isoformat(),
        "safety": {
            "read_only": True,
            "host_networking_changed": False,
            "requires_operator_review": not ready_to_apply,
            "b1_static_ip_configures": False,
            "static_host_infrastructure_preserved": bool(static_candidate_addresses),
        },
        "status": status,
        "ready_to_apply": ready_to_apply,
        "appliance_hostname": appliance_hostname,
        "hostname_authority": "b1-appliance-config",
        "network_property_source": "host-dhcp-client",
        "b1_static_ip_configures": False,
        "ordinary_dhcp_lease_count": len(required_set),
        "dns_admin": dns_admin_summary(dns_admin_url),
        "connection": profile,
        "dhcp_reserved_appliance_addresses": required_addresses,
        "required_dhcp_reservations": required_addresses,
        "host_infrastructure_static_addresses": static_candidate_addresses,
        "reservation_confirmed": reservation_confirmed,
        "candidate_connection": candidate,
        "warnings": warnings,
        "blockers": blockers,
        "commands": migration_commands(
            connection,
            candidate,
            [item["cidr"] for item in static_candidate_addresses if item.get("cidr")],
        ),
        "operator_note": (
            "This plan is intentionally non-destructive. Keep static host-infrastructure addresses such as Technitium DHCP/DNS "
            "on the interface, confirm the B1 appliance DHCP reservation in Technitium or the LAN DHCP server first, then run "
            "the staged NetworkManager candidate commands during a console-access maintenance window."
        ),
    }


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    target = path.expanduser()
    parent = target.parent
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise NetworkDhcpPlanError(f"output must be a regular file, not a symlink or special file: {target}")
    for ancestor in [parent, *parent.parents]:
        if ancestor.exists() and ancestor.is_symlink():
            raise NetworkDhcpPlanError(f"output parent directory must not be a symlink: {ancestor}")
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a read-only DHCP migration plan for the B1 appliance host network.")
    parser.add_argument("--connection", default=os.getenv("B1_NETWORK_CONNECTION", "lan-dhcp-dns"))
    parser.add_argument(
        "--appliance-hostname",
        default=os.getenv("B1_APPLIANCE_HOSTNAME") or os.getenv("B1_EXPECTED_TARGET_HOST") or "ai.b1.germering",
    )
    parser.add_argument("--dns-admin-url", default=os.getenv("B1_DNS_ADMIN_URL", ""))
    parser.add_argument(
        "--required-address",
        action="append",
        default=[],
        help="Required DHCP-reserved IPv4 address, optionally ADDRESS=purpose. May be repeated.",
    )
    parser.add_argument(
        "--static-infrastructure-address",
        action="append",
        default=[],
        help="Static host-infrastructure IPv4 address to preserve, optionally ADDRESS[/PREFIX]=purpose. May be repeated.",
    )
    parser.add_argument(
        "--reservation-confirmed",
        action="store_true",
        default=env_bool("B1_DHCP_RESERVATION_CONFIRMED"),
        help="Operator has confirmed matching DHCP reservations.",
    )
    parser.add_argument("--output", default=os.getenv("B1_NETWORK_DHCP_PLAN", ""))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        required = parse_required_addresses(args.required_address, os.getenv("B1_DHCP_REQUIRED_ADDRESSES", ""))
        static_infrastructure = parse_static_infrastructure_addresses(
            args.static_infrastructure_address,
            os.getenv("B1_STATIC_INFRASTRUCTURE_ADDRESSES", ""),
        )
        plan = build_plan(
            connection=args.connection,
            appliance_hostname=args.appliance_hostname,
            dns_admin_url=args.dns_admin_url,
            required_addresses=required,
            static_infrastructure_addresses=static_infrastructure,
            reservation_confirmed=args.reservation_confirmed,
        )
    except NetworkDhcpPlanError as exc:
        raise SystemExit(str(exc)) from exc
    if args.output:
        write_private_json(Path(args.output), plan)
        print(f"wrote network DHCP plan: {args.output}")
    else:
        print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
