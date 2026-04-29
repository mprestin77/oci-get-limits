#!/usr/bin/env python3
"""
Show OCI service limits and current usage for a tenancy.

The script discovers OCI services, lists each service's limits, and shows
current usage when OCI exposes it. If OCI does not provide usage for a limit,
the script shows N/A.
"""

import argparse
import sys

import oci


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Show OCI service limits and current usage for your tenancy. "
            "For limits where OCI does not expose current usage, the script shows N/A."
        ),
        epilog=(
            "Examples:\n"
            "  python3 get-limits.py\n"
            "  python3 get-limits.py --service compute\n"
            "  python3 get-limits.py --service identity --region us-ashburn-1\n"
            "  python3 get-limits.py --only-with-usage\n"
            "  python3 get-limits.py --profile PROD --config-file ~/.oci/config"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-s",
        "--service",
        help="Only show limits for one OCI service (for example: compute, identity).",
    )
    parser.add_argument(
        "-r",
        "--region",
        help="Override the region from your OCI config.",
    )
    parser.add_argument(
        "-p",
        "--profile",
        default="DEFAULT",
        help="OCI config profile name. Default: DEFAULT",
    )
    parser.add_argument(
        "-c",
        "--config-file",
        default=oci.config.DEFAULT_LOCATION,
        help=f"OCI config path. Default: {oci.config.DEFAULT_LOCATION}",
    )
    parser.add_argument(
        "--only-with-usage",
        action="store_true",
        help="Only show limits where OCI exposes current usage or the script can count it manually.",
    )
    return parser.parse_args()


def ellipsize(value, width):
    text = str(value)
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def format_number(value):
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.2f}"
    return f"{value:,}"


def percent_used(used, limit_value):
    if used is None or limit_value in (None, 0):
        return "N/A"
    return f"{(used / limit_value) * 100:.1f}%"


def paged(callable_obj, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(callable_obj, *args, **kwargs).data


def safe_list(callable_obj, *args, **kwargs):
    try:
        return paged(callable_obj, *args, **kwargs)
    except Exception:
        return []


def get_identity_manual_usage(identity_client, tenancy_id):
    usage = {}

    users = safe_list(identity_client.list_users, tenancy_id)
    groups = safe_list(identity_client.list_groups, tenancy_id)
    dynamic_groups = safe_list(identity_client.list_dynamic_groups, tenancy_id)
    network_sources = safe_list(identity_client.list_network_sources, tenancy_id)
    domains = safe_list(identity_client.list_domains, tenancy_id)

    usage["users-count"] = len(users)
    usage["groups-count"] = len(groups)
    usage["dynamic-groups-count"] = len(dynamic_groups)
    usage["network-sources-count"] = len(network_sources)

    domain_usage = {
        "free-domains-count": 0,
        "premium-domains-count": 0,
        "oracle-apps-domains-count": 0,
        "oracle-apps-premium-domains-count": 0,
        "external-user-domains-count": 0,
    }
    for domain in domains:
        license_type = (getattr(domain, "license_type", "") or "").upper()
        if license_type == "FREE":
            domain_usage["free-domains-count"] += 1
        elif license_type == "PREMIUM":
            domain_usage["premium-domains-count"] += 1
        elif license_type == "ORACLE_APPS":
            domain_usage["oracle-apps-domains-count"] += 1
        elif license_type == "ORACLE_APPS_PREMIUM":
            domain_usage["oracle-apps-premium-domains-count"] += 1
        elif license_type == "EXTERNAL_USER":
            domain_usage["external-user-domains-count"] += 1
    usage.update(domain_usage)

    compartments = safe_list(
        identity_client.list_compartments,
        tenancy_id,
        compartment_id_in_subtree=True,
        access_level="ACCESSIBLE",
    )
    active_compartment_ids = [
        compartment.id
        for compartment in compartments
        if getattr(compartment, "lifecycle_state", "") == "ACTIVE"
    ]

    policy_count = 0
    statement_count = 0
    for compartment_id in [tenancy_id] + active_compartment_ids:
        try:
            policies = paged(identity_client.list_policies, compartment_id)
        except Exception:
            continue
        policy_count += len(policies)
        statement_count += sum(len(policy.statements or []) for policy in policies)

    usage["policies-count"] = policy_count
    usage["statements-count"] = statement_count
    return usage


def fetch_usage(
    limits_client,
    tenancy_id,
    service_name,
    limit_value,
    identity_usage,
    usage_supported,
):
    if service_name.lower() == "identity" and limit_value.name in identity_usage:
        return {
            "used": identity_usage[limit_value.name],
            "available": None,
            "quota": None,
            "source": "manual",
        }

    if not usage_supported:
        return {
            "used": None,
            "available": None,
            "quota": None,
            "source": "unsupported",
        }

    kwargs = {}
    if limit_value.availability_domain:
        kwargs["availability_domain"] = limit_value.availability_domain

    try:
        availability = limits_client.get_resource_availability(
            service_name=service_name,
            limit_name=limit_value.name,
            compartment_id=tenancy_id,
            **kwargs,
        ).data
    except oci.exceptions.ServiceError as exc:
        if exc.status in (400, 404, 409):
            return {
                "used": None,
                "available": None,
                "quota": None,
                "source": "unavailable",
            }
        raise

    return {
        "used": availability.used,
        "available": availability.available,
        "quota": availability.effective_quota_value,
        "source": "api",
    }


def service_names(limits_client, tenancy_id, selected_service):
    if selected_service:
        return [selected_service]

    services = paged(limits_client.list_services, tenancy_id)
    return sorted(service.name for service in services)


def limit_definitions_by_name(limits_client, tenancy_id, service_name):
    definitions = paged(
        limits_client.list_limit_definitions,
        tenancy_id,
        service_name=service_name,
    )
    return {definition.name: definition for definition in definitions}


def print_row(service_name, limit_value, usage):
    scope = limit_value.availability_domain or limit_value.scope_type or "GLOBAL"
    quota = usage["quota"]
    effective_max = quota if quota is not None else limit_value.value

    print(
        f"{ellipsize(service_name, 18):<18} "
        f"{ellipsize(limit_value.name, 42):<42} "
        f"{ellipsize(scope, 18):<18} "
        f"{format_number(limit_value.value):>12} "
        f"{format_number(usage['used']):>12} "
        f"{percent_used(usage['used'], effective_max):>8}"
    )


def main():
    args = parse_args()

    try:
        config = oci.config.from_file(args.config_file, args.profile)
    except Exception as exc:
        print(f"Failed to load OCI config: {exc}", file=sys.stderr)
        return 1

    if args.region:
        config["region"] = args.region

    tenancy_id = config["tenancy"]
    limits_client = oci.limits.LimitsClient(config)
    identity_client = oci.identity.IdentityClient(config)

    try:
        services = service_names(limits_client, tenancy_id, args.service)
    except Exception as exc:
        print(f"Failed to list OCI services: {exc}", file=sys.stderr)
        return 1

    identity_usage = {}
    if not args.service or args.service.lower() == "identity":
        identity_usage = get_identity_manual_usage(identity_client, tenancy_id)

    print(f"Region: {config.get('region', 'unknown')}")
    print(
        f"{'SERVICE':<18} {'LIMIT':<42} {'SCOPE':<18} "
        f"{'MAX':>12} {'USED':>12} {'USED%':>8}"
    )
    print("-" * 116)

    printed_rows = 0
    failed_services = []

    for service_name in services:
        try:
            definitions = limit_definitions_by_name(limits_client, tenancy_id, service_name)
            limit_values = paged(
                limits_client.list_limit_values,
                tenancy_id,
                service_name=service_name,
            )
        except Exception as exc:
            failed_services.append((service_name, str(exc)))
            continue

        for limit_value in sorted(
            limit_values,
            key=lambda item: (
                item.name.lower(),
                item.availability_domain or "",
                item.scope_type or "",
            ),
        ):
            definition = definitions.get(limit_value.name)
            usage_supported = (
                service_name.lower() == "identity" and limit_value.name in identity_usage
            ) or bool(
                definition and getattr(definition, "is_resource_availability_supported", False)
            )

            try:
                usage = fetch_usage(
                    limits_client,
                    tenancy_id,
                    service_name,
                    limit_value,
                    identity_usage,
                    usage_supported,
                )
            except Exception as exc:
                usage = {
                    "used": None,
                    "available": None,
                    "quota": None,
                    "source": f"error:{type(exc).__name__}",
                }

            if args.only_with_usage and usage["used"] is None:
                continue

            print_row(service_name, limit_value, usage)
            printed_rows += 1

    if failed_services:
        print("\nServices that could not be queried:", file=sys.stderr)
        for service_name, error_text in failed_services:
            print(f"  {service_name}: {error_text}", file=sys.stderr)

    if printed_rows == 0:
        print(
            "No rows returned. Retry without --only-with-usage to include limits that do not expose live usage.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
