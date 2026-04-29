#!/usr/bin/env python3
"""
Show OCI service limits and current usage for a tenancy.

This version extends the generic OCI Limits API with service-specific collectors
for limits that often do not report usage through `get_resource_availability`.
It currently adds manual usage for selected `identity`, `certificates`, and
`batch-computing` limits.
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import oci

DEFAULT_SCRIPT_SETTINGS = {
    "IDENTITY_POLICY_STATEMENTS_PER_HIERARCHY_LIMIT": 500,
}


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Show OCI service limits and current usage for your tenancy. "
            "For limits where OCI does not expose current usage and the script "
            "cannot count it directly, the script shows N/A."
        ),
        epilog=(
            "Examples:\n"
            "  python3 get-limits-service-usage.py\n"
            "  python3 get-limits-service-usage.py --list-services\n"
            "  python3 get-limits-service-usage.py --service certificates\n"
            "  python3 get-limits-service-usage.py --services-file services.conf\n"
            "  python3 get-limits-service-usage.py --settings-file limits-settings.conf\n"
            "  python3 get-limits-service-usage.py --service batch-computing --region us-ashburn-1\n"
            "  python3 get-limits-service-usage.py --only-with-usage\n"
            "  python3 get-limits-service-usage.py --profile PROD --config-file ~/.oci/config"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    selection_group = parser.add_mutually_exclusive_group()
    selection_group.add_argument(
        "-s",
        "--service",
        help="Only show limits for one OCI service (for example: compute, certificates).",
    )
    selection_group.add_argument(
        "--services-file",
        help="Path to a plain-text file with one OCI service name per line. Lines starting with # are ignored.",
    )
    selection_group.add_argument(
        "--list-services",
        action="store_true",
        help="List all OCI services returned by the Limits API and exit.",
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
        "--settings-file",
        default="limits-settings.conf",
        help="Path to a simple KEY = VALUE settings file. Default: limits-settings.conf",
    )
    parser.add_argument(
        "--only-with-usage",
        action="store_true",
        help="Only show limits where the script determined usage and the used value is greater than zero.",
    )
    return parser.parse_args()


def ellipsize(value, width):
    text = str(value)
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def format_number(value, limit_name=""):
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.2f}"
    return f"{value:,}"


def percent_used(used, limit_value, limit_name=""):
    if used is None or limit_value in (None, 0):
        return "N/A"
    return f"{(used / limit_value) * 100:.1f}%"


def paged(callable_obj, *args, **kwargs):
    return oci.pagination.list_call_get_all_results(callable_obj, *args, **kwargs).data


def safe_paged(callable_obj, *args, **kwargs):
    try:
        return paged(callable_obj, *args, **kwargs)
    except Exception:
        return []


def paged_response_items(callable_obj, *args, **kwargs):
    items = []
    page = None
    while True:
        response = callable_obj(*args, page=page, **kwargs) if page else callable_obj(*args, **kwargs)
        items.extend(getattr(response.data, "items", []) or [])
        page = response.headers.get("opc-next-page")
        if not page:
            return items


def load_script_settings(path_text):
    settings = dict(DEFAULT_SCRIPT_SETTINGS)
    path = Path(path_text).expanduser()
    if not path.exists():
        return settings

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        raise RuntimeError(f"Failed to read settings file {path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(
                f"Invalid settings line {line_number} in {path}: expected KEY = VALUE"
            )
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in settings:
            raise RuntimeError(f"Unknown setting {key} in {path}")
        try:
            settings[key] = int(value)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid integer value for {key} in {path}: {value}"
            ) from exc
    return settings


def active_compartment_ids(identity_client, tenancy_id):
    compartments = safe_paged(
        identity_client.list_compartments,
        tenancy_id,
        compartment_id_in_subtree=True,
        access_level="ACCESSIBLE",
    )
    ids = [tenancy_id]
    for compartment in compartments:
        if getattr(compartment, "lifecycle_state", "") == "ACTIVE":
            ids.append(compartment.id)
    seen = set()
    ordered = []
    for compartment_id in ids:
        if compartment_id in seen:
            continue
        seen.add(compartment_id)
        ordered.append(compartment_id)
    return ordered


def dedupe_by_id(resources):
    seen = set()
    deduped = []
    for resource in resources:
        resource_id = getattr(resource, "id", None)
        if resource_id is None:
            deduped.append(resource)
            continue
        if resource_id in seen:
            continue
        seen.add(resource_id)
        deduped.append(resource)
    return deduped


def is_deleted(resource):
    return (getattr(resource, "lifecycle_state", "") or "").upper() == "DELETED"


def state_upper(resource):
    return (getattr(resource, "lifecycle_state", "") or "").upper()


def exclude_states(resources, *states):
    excluded = {state.upper() for state in states}
    return [resource for resource in resources if state_upper(resource) not in excluded]


def list_by_compartments(callable_obj, compartment_ids, filter_deleted=False, **kwargs):
    resources = []
    for compartment_id in compartment_ids:
        items = safe_paged(callable_obj, compartment_id=compartment_id, **kwargs)
        for item in items:
            if filter_deleted and is_deleted(item):
                continue
            resources.append(item)
    return dedupe_by_id(resources)


def get_identity_manual_usage(identity_client, tenancy_id, compartment_ids):
    usage = {}

    users = safe_paged(identity_client.list_users, tenancy_id)
    groups = safe_paged(identity_client.list_groups, tenancy_id)
    dynamic_groups = safe_paged(identity_client.list_dynamic_groups, tenancy_id)
    network_sources = safe_paged(identity_client.list_network_sources, tenancy_id)
    domains = safe_paged(identity_client.list_domains, tenancy_id)

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

    compartments = safe_paged(
        identity_client.list_compartments,
        tenancy_id,
        compartment_id_in_subtree=True,
        access_level="ACCESSIBLE",
    )
    active_compartments = [
        compartment
        for compartment in compartments
        if getattr(compartment, "lifecycle_state", "") == "ACTIVE"
    ]

    policy_count = 0
    statement_count = 0
    statements_by_compartment = defaultdict(int)
    for compartment_id in compartment_ids:
        try:
            policies = paged(identity_client.list_policies, compartment_id)
        except Exception:
            continue
        policy_count += len(policies)
        direct_statement_count = sum(len(policy.statements or []) for policy in policies)
        statements_by_compartment[compartment_id] += direct_statement_count
        statement_count = max(
            statement_count,
            max((len(policy.statements or []) for policy in policies), default=0),
        )

    usage["policies-count"] = policy_count
    usage["statements-count"] = statement_count

    children_by_parent = defaultdict(list)
    for compartment in active_compartments:
        parent_id = getattr(compartment, "compartment_id", None) or tenancy_id
        children_by_parent[parent_id].append(compartment.id)

    max_hierarchy_statements = 0
    stack = [(tenancy_id, statements_by_compartment.get(tenancy_id, 0))]
    while stack:
        compartment_id, cumulative_statements = stack.pop()
        if cumulative_statements > max_hierarchy_statements:
            max_hierarchy_statements = cumulative_statements
        for child_id in children_by_parent.get(compartment_id, []):
            stack.append(
                (
                    child_id,
                    cumulative_statements + statements_by_compartment.get(child_id, 0),
                )
            )

    usage["policy-statements-per-compartment-hierarchy"] = max_hierarchy_statements
    return usage


def get_certificates_manual_usage(config, compartment_ids):
    client = oci.certificates_management.CertificatesManagementClient(config)

    certificates = list_by_compartments(
        client.list_certificates,
        compartment_ids,
        filter_deleted=True,
    )
    certificate_authorities = list_by_compartments(
        client.list_certificate_authorities,
        compartment_ids,
        filter_deleted=True,
    )
    ca_bundles = list_by_compartments(
        client.list_ca_bundles,
        compartment_ids,
        filter_deleted=True,
    )
    associations = list_by_compartments(
        client.list_associations,
        compartment_ids,
        filter_deleted=True,
    )

    versions_per_resource = []
    scheduled_deletion_versions_per_resource = []

    for certificate in certificates:
        versions = safe_paged(client.list_certificate_versions, certificate.id)
        versions_per_resource.append(len(versions))
        scheduled_deletion_versions_per_resource.append(
            sum(1 for version in versions if getattr(version, "time_of_deletion", None))
        )

    for certificate_authority in certificate_authorities:
        versions = safe_paged(
            client.list_certificate_authority_versions,
            certificate_authority.id,
        )
        versions_per_resource.append(len(versions))
        scheduled_deletion_versions_per_resource.append(
            sum(1 for version in versions if getattr(version, "time_of_deletion", None))
        )

    associations_by_resource = defaultdict(int)
    for association in associations:
        resource_id = getattr(association, "certificates_resource_id", None)
        if resource_id:
            associations_by_resource[resource_id] += 1

    return {
        "ca-bundles-count": len(ca_bundles),
        "certificate-authorities-count": len(certificate_authorities),
        "certificates-count": len(certificates),
        "associations-per-certificates-resource-count": max(
            associations_by_resource.values(), default=0
        ),
        "versions-per-certificates-resource-count": max(
            versions_per_resource,
            default=0,
        ),
        "versions-scheduled-deletion-per-certificates-resource-count": max(
            scheduled_deletion_versions_per_resource,
            default=0,
        ),
    }


def get_batch_manual_usage(config, compartment_ids):
    client = oci.batch.BatchComputingClient(config)

    contexts = list_by_compartments(
        client.list_batch_contexts,
        compartment_ids,
        filter_deleted=True,
    )
    job_pools = list_by_compartments(
        client.list_batch_job_pools,
        compartment_ids,
        filter_deleted=True,
    )
    jobs = list_by_compartments(client.list_batch_jobs, compartment_ids)
    tasks = list_by_compartments(client.list_batch_tasks, compartment_ids)
    task_environments = list_by_compartments(
        client.list_batch_task_environments,
        compartment_ids,
        filter_deleted=True,
    )
    task_profiles = list_by_compartments(
        client.list_batch_task_profiles,
        compartment_ids,
        filter_deleted=True,
    )

    jobs_by_id = {job.id: job for job in jobs if getattr(job, "id", None)}

    job_pools_by_context = defaultdict(int)
    for job_pool in job_pools:
        context_id = getattr(job_pool, "batch_context_id", None)
        if context_id:
            job_pools_by_context[context_id] += 1

    tasks_by_job = defaultdict(int)
    active_tasks_by_context = defaultdict(int)
    active_tasks_by_pool = defaultdict(int)
    active_task_states = {"ACCEPTED", "WAITING", "IN_PROGRESS"}

    for task in tasks:
        job_id = getattr(task, "job_id", None)
        if job_id:
            tasks_by_job[job_id] += 1

        if (getattr(task, "lifecycle_state", "") or "").upper() not in active_task_states:
            continue

        job = jobs_by_id.get(job_id)
        if not job:
            continue

        context_id = getattr(job, "batch_context_id", None)
        if context_id:
            active_tasks_by_context[context_id] += 1

        batch_job_pool_id = getattr(job, "batch_job_pool_id", None)
        if batch_job_pool_id:
            active_tasks_by_pool[batch_job_pool_id] += 1

    return {
        "batch-context-count": len(contexts),
        "batch-environment-count": len(task_environments),
        "batch-fleet-per-context-count": max(job_pools_by_context.values(), default=0),
        "batch-job-count": len(jobs),
        "batch-jobpool-count": len(job_pools),
        "batch-task-count": len(tasks),
        "batch-task-profile-count": len(task_profiles),
        "batch-tasks-per-job-count": max(tasks_by_job.values(), default=0),
        "batch-concurrent-tasks-per-context-count": max(
            active_tasks_by_context.values(),
            default=0,
        ),
        "batch-concurrent-tasks-per-fleet-count": max(
            active_tasks_by_pool.values(),
            default=0,
        ),
    }


def get_block_storage_manual_usage(config, compartment_ids):
    client = oci.core.BlockstorageClient(config)
    volume_groups = list_by_compartments(
        client.list_volume_groups,
        compartment_ids,
        filter_deleted=True,
    )
    return {
        "volumes-per-group": max(
            (len(getattr(group, "volume_ids", []) or []) for group in volume_groups),
            default=0,
        )
    }


def get_vcn_manual_usage(config, compartment_ids):
    client = oci.core.VirtualNetworkClient(config)

    dhcp_options = list_by_compartments(client.list_dhcp_options, compartment_ids, filter_deleted=True)
    drgs = list_by_compartments(client.list_drgs, compartment_ids, filter_deleted=True)
    internet_gateways = list_by_compartments(
        client.list_internet_gateways,
        compartment_ids,
        filter_deleted=True,
    )
    nat_gateways = list_by_compartments(client.list_nat_gateways, compartment_ids, filter_deleted=True)
    route_tables = list_by_compartments(client.list_route_tables, compartment_ids, filter_deleted=True)
    security_lists = list_by_compartments(
        client.list_security_lists,
        compartment_ids,
        filter_deleted=True,
    )
    subnets = list_by_compartments(client.list_subnets, compartment_ids, filter_deleted=True)
    network_security_groups = list_by_compartments(
        client.list_network_security_groups,
        compartment_ids,
        filter_deleted=True,
    )

    dhcp_options_by_vcn = defaultdict(int)
    for resource in dhcp_options:
        if getattr(resource, "vcn_id", None):
            dhcp_options_by_vcn[resource.vcn_id] += 1

    internet_gateways_by_vcn = defaultdict(int)
    for resource in internet_gateways:
        if getattr(resource, "vcn_id", None):
            internet_gateways_by_vcn[resource.vcn_id] += 1

    nat_gateways_by_vcn = defaultdict(int)
    for resource in nat_gateways:
        if getattr(resource, "vcn_id", None):
            nat_gateways_by_vcn[resource.vcn_id] += 1

    route_tables_by_vcn = defaultdict(int)
    for resource in route_tables:
        if getattr(resource, "vcn_id", None):
            route_tables_by_vcn[resource.vcn_id] += 1

    security_lists_by_vcn = defaultdict(int)
    for resource in security_lists:
        if getattr(resource, "vcn_id", None):
            security_lists_by_vcn[resource.vcn_id] += 1

    subnets_by_vcn = defaultdict(int)
    for resource in subnets:
        if getattr(resource, "vcn_id", None):
            subnets_by_vcn[resource.vcn_id] += 1

    nsgs_by_vcn = defaultdict(int)
    for resource in network_security_groups:
        if getattr(resource, "vcn_id", None):
            nsgs_by_vcn[resource.vcn_id] += 1

    nsg_rule_counts = []
    for nsg in network_security_groups:
        rules = safe_paged(client.list_network_security_group_security_rules, nsg.id)
        nsg_rule_counts.append(len(rules))

    return {
        "dhcp-option-count": max(dhcp_options_by_vcn.values(), default=0),
        "drg-count": len(drgs),
        "internet-gateway-count": max(internet_gateways_by_vcn.values(), default=0),
        "nat-gateway-count": max(nat_gateways_by_vcn.values(), default=0),
        "networksecuritygroups-count": max(nsgs_by_vcn.values(), default=0),
        "route-table-count": max(route_tables_by_vcn.values(), default=0),
        "security-list-count": max(security_lists_by_vcn.values(), default=0),
        "securityrules-per-networksecuritygroup-count": max(nsg_rule_counts, default=0),
        "subnet-count": max(subnets_by_vcn.values(), default=0),
    }


def get_fast_connect_manual_usage(config, compartment_ids):
    client = oci.core.VirtualNetworkClient(config)

    cross_connects = exclude_states(
        list_by_compartments(client.list_cross_connects, compartment_ids),
        "TERMINATED",
        "TERMINATING",
    )
    cross_connect_groups = exclude_states(
        list_by_compartments(client.list_cross_connect_groups, compartment_ids),
        "TERMINATED",
        "TERMINATING",
    )
    virtual_circuits = exclude_states(
        list_by_compartments(client.list_virtual_circuits, compartment_ids),
        "TERMINATED",
        "TERMINATING",
    )
    remote_peering_connections = exclude_states(
        list_by_compartments(client.list_remote_peering_connections, compartment_ids),
        "TERMINATED",
        "TERMINATING",
    )

    speed_counts = {
        "1": 0,
        "10": 0,
        "100": 0,
        "400": 0,
    }
    pending_loa_count = 0

    for cross_connect in cross_connects:
        digits = "".join(character for character in (cross_connect.port_speed_shape_name or "") if character.isdigit())
        if digits in speed_counts:
            speed_counts[digits] += 1
        if state_upper(cross_connect) == "PENDING_CUSTOMER":
            pending_loa_count += 1

    return {
        "cross-connect-1g-count": speed_counts["1"],
        "cross-connect-10g-count": speed_counts["10"],
        "cross-connect-100g-count": speed_counts["100"],
        "cross-connect-400g-count": speed_counts["400"],
        "cross-connect-count": len(cross_connects),
        "cross-connect-group-count": len(cross_connect_groups),
        "cross-connect-pending-loa-count": pending_loa_count,
        "remote-peering-connection-count": len(remote_peering_connections),
        "virtual-circuit-count": len(virtual_circuits),
    }


def get_dns_manual_usage(config, compartment_ids):
    client = oci.dns.DnsClient(config)

    zones = list_by_compartments(client.list_zones, compartment_ids, filter_deleted=True)
    steering_policies = list_by_compartments(
        client.list_steering_policies,
        compartment_ids,
        filter_deleted=True,
    )
    steering_policy_attachments = list_by_compartments(
        client.list_steering_policy_attachments,
        compartment_ids,
        filter_deleted=True,
    )
    tsig_keys = list_by_compartments(client.list_tsig_keys, compartment_ids, filter_deleted=True)

    records_count = 0
    records_visible = False
    for zone in zones:
        zone_id = getattr(zone, "id", None)
        if not zone_id:
            continue
        kwargs = {}
        scope = (getattr(zone, "scope", "") or "").upper()
        if scope:
            kwargs["scope"] = scope
        if scope == "PRIVATE" and getattr(zone, "view_id", None):
            kwargs["view_id"] = zone.view_id
        try:
            records = paged_response_items(client.get_zone_records, zone_id, **kwargs)
        except Exception:
            continue
        records_visible = True
        records_count += len(records)

    usage = {
        "global-zone-count": sum(1 for zone in zones if (getattr(zone, "scope", "") or "").upper() == "GLOBAL"),
        "steering-policy-attachment-count": len(steering_policy_attachments),
        "steering-policy-count": len(steering_policies),
        "tsig-key-count": len(tsig_keys),
    }
    if records_visible:
        usage["records-count"] = records_count
    return usage


def get_notifications_manual_usage(config, compartment_ids):
    control_client = oci.ons.NotificationControlPlaneClient(config)
    data_client = oci.ons.NotificationDataPlaneClient(config)

    topics = list_by_compartments(control_client.list_topics, compartment_ids, filter_deleted=True)
    subscriptions = list_by_compartments(
        data_client.list_subscriptions,
        compartment_ids,
        filter_deleted=True,
    )

    return {
        "subscription-count": len(subscriptions),
        "topic-count": len(topics),
    }


def get_faas_manual_usage(config, compartment_ids):
    client = oci.functions.FunctionsManagementClient(config)

    applications = list_by_compartments(client.list_applications, compartment_ids, filter_deleted=True)
    functions = []
    for application in applications:
        functions.extend(safe_paged(client.list_functions, application.id))
    functions = dedupe_by_id(
        [function for function in functions if not is_deleted(function)]
    )

    provisioned_concurrency_mb = 0
    for function_summary in functions:
        function_id = getattr(function_summary, "id", None)
        if not function_id:
            continue
        try:
            function = client.get_function(function_id).data
        except Exception:
            function = function_summary
        config_obj = getattr(function, "provisioned_concurrency_config", None)
        count = getattr(config_obj, "count", None)
        memory_in_mbs = getattr(function, "memory_in_mbs", None)
        if count is None or memory_in_mbs is None:
            continue
        provisioned_concurrency_mb += count * memory_in_mbs

    return {
        "application-count": len(applications),
        "function-count": len(functions),
        "provisioned-concurrency-mb": provisioned_concurrency_mb,
    }


def get_load_balancer_manual_usage(config, compartment_ids):
    client = oci.load_balancer.LoadBalancerClient(config)
    load_balancer_summaries = list_by_compartments(
        client.list_load_balancers,
        compartment_ids,
        filter_deleted=True,
    )

    backend_sets_per_lb = []
    hostnames_per_lb = []
    listeners_per_lb = []
    max_rules_per_lb = []

    for summary in load_balancer_summaries:
        try:
            load_balancer = client.get_load_balancer(summary.id).data
        except Exception:
            continue
        backend_sets_per_lb.append(len(getattr(load_balancer, "backend_sets", {}) or {}))
        hostnames_per_lb.append(len(getattr(load_balancer, "hostnames", {}) or {}))
        listeners_per_lb.append(len(getattr(load_balancer, "listeners", {}) or {}))
        rule_set_items = sum(
            len(getattr(rule_set, "items", []) or [])
            for rule_set in (getattr(load_balancer, "rule_sets", {}) or {}).values()
        )
        routing_policy_rules = sum(
            len(getattr(policy, "rules", []) or [])
            for policy in (getattr(load_balancer, "routing_policies", {}) or {}).values()
        )
        max_rules_per_lb.append(rule_set_items + routing_policy_rules)

    return {
        "backend-sets-per-lb-count": max(backend_sets_per_lb, default=0),
        "hostnames-per-lb-count": max(hostnames_per_lb, default=0),
        "lb-max-rules-count": max(max_rules_per_lb, default=0),
        "listeners-per-lb-count": max(listeners_per_lb, default=0),
    }


def get_secrets_manual_usage(config, compartment_ids):
    client = oci.vault.VaultsClient(config)
    secrets = list_by_compartments(client.list_secrets, compartment_ids, filter_deleted=True)

    versions_per_secret = []
    scheduled_deletion_per_secret = []
    for secret in secrets:
        versions = safe_paged(client.list_secret_versions, secret.id)
        versions_per_secret.append(len(versions))
        scheduled_deletion_per_secret.append(
            sum(1 for version in versions if getattr(version, "time_of_deletion", None))
        )

    return {
        "max-versions-per-secret-count": max(versions_per_secret, default=0),
        "max-versions-scheduled-deletion-per-secret-count": max(
            scheduled_deletion_per_secret,
            default=0,
        ),
    }


def get_resource_scheduler_manual_usage(config, compartment_ids):
    client = oci.resource_scheduler.ScheduleClient(config)
    schedules = list_by_compartments(client.list_schedules, compartment_ids, filter_deleted=True)
    return {
        "schedule-count": len(schedules),
    }


def get_regions_manual_usage(identity_client, tenancy_id):
    subscriptions = safe_paged(identity_client.list_region_subscriptions, tenancy_id)
    return {
        "subscribed-region-count": len(subscriptions),
    }


def get_vcn_logging_usage(config, compartment_ids):
    logging_client = oci.logging.LoggingManagementClient(config)
    log_groups = list_by_compartments(
        logging_client.list_log_groups,
        compartment_ids,
        filter_deleted=True,
    )

    flow_log_count = 0
    for log_group in log_groups:
        logs = safe_paged(logging_client.list_logs, log_group.id)
        for log in logs:
            if is_deleted(log):
                continue
            configuration = getattr(log, "configuration", None)
            source = getattr(configuration, "source", None)
            service = (getattr(source, "service", "") or "").lower()
            if service == "flowlogs":
                flow_log_count += 1

    return {
        "flow-log-config-count": flow_log_count,
    }


def get_container_engine_manual_usage(config, compartment_ids):
    client = oci.container_engine.ContainerEngineClient(config)
    node_pools = list_by_compartments(client.list_node_pools, compartment_ids, filter_deleted=True)
    virtual_node_pools = list_by_compartments(
        client.list_virtual_node_pools,
        compartment_ids,
        filter_deleted=True,
    )

    managed_nodes = 0
    for pool in node_pools:
        node_config = getattr(pool, "node_config_details", None)
        if node_config and getattr(node_config, "size", None) is not None:
            managed_nodes += node_config.size
            continue
        quantity_per_subnet = getattr(pool, "quantity_per_subnet", None)
        subnet_ids = getattr(pool, "subnet_ids", None) or []
        if quantity_per_subnet is not None:
            managed_nodes += quantity_per_subnet * max(len(subnet_ids), 1)

    virtual_nodes = sum(getattr(pool, "size", 0) or 0 for pool in virtual_node_pools)

    return {
        "node-count": managed_nodes + virtual_nodes,
    }


def manual_usage_by_service(config, identity_client, tenancy_id, compartment_ids, service_name):
    normalized = service_name.lower()
    if normalized == "identity":
        return get_identity_manual_usage(identity_client, tenancy_id, compartment_ids)
    if normalized == "certificates":
        return get_certificates_manual_usage(config, compartment_ids)
    if normalized == "batch-computing":
        return get_batch_manual_usage(config, compartment_ids)
    if normalized == "block-storage":
        return get_block_storage_manual_usage(config, compartment_ids)
    if normalized == "vcn":
        usage = get_vcn_manual_usage(config, compartment_ids)
        usage.update(get_vcn_logging_usage(config, compartment_ids))
        return usage
    if normalized == "fast-connect":
        return get_fast_connect_manual_usage(config, compartment_ids)
    if normalized == "dns":
        return get_dns_manual_usage(config, compartment_ids)
    if normalized == "notifications":
        return get_notifications_manual_usage(config, compartment_ids)
    if normalized == "faas":
        return get_faas_manual_usage(config, compartment_ids)
    if normalized == "load-balancer":
        return get_load_balancer_manual_usage(config, compartment_ids)
    if normalized == "secrets":
        return get_secrets_manual_usage(config, compartment_ids)
    if normalized == "resource-scheduler":
        return get_resource_scheduler_manual_usage(config, compartment_ids)
    if normalized == "regions":
        return get_regions_manual_usage(identity_client, tenancy_id)
    if normalized == "container-engine":
        return get_container_engine_manual_usage(config, compartment_ids)
    return {}


def fetch_usage(
    limits_client,
    tenancy_id,
    service_name,
    limit_value,
    manual_usage,
    usage_supported,
):
    if limit_value.name in manual_usage:
        return {
            "used": manual_usage[limit_value.name],
            "quota": None,
        }

    if not usage_supported:
        return {
            "used": None,
            "quota": None,
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
                "quota": None,
            }
        raise

    return {
        "used": availability.used,
        "quota": availability.effective_quota_value,
    }


def service_names(limits_client, tenancy_id, selected_service):
    if selected_service:
        return [selected_service]

    services = paged(limits_client.list_services, tenancy_id)
    return sorted(service.name for service in services)


def read_services_file(path_text):
    path = Path(path_text).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        raise RuntimeError(f"Failed to read services file {path}: {exc}") from exc

    services = []
    seen = set()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            continue
        seen.add(line)
        services.append(line)
    return services


def resolve_target_services(args, available_services):
    if args.service:
        return [args.service]
    if args.services_file:
        configured_services = read_services_file(args.services_file)
        unknown_services = [service for service in configured_services if service not in available_services]
        if unknown_services:
            raise RuntimeError(
                "Unknown services in services file: " + ", ".join(sorted(unknown_services))
            )
        return configured_services
    return available_services


def limit_definitions_by_name(limits_client, tenancy_id, service_name):
    definitions = paged(
        limits_client.list_limit_definitions,
        tenancy_id,
        service_name=service_name,
    )
    return {definition.name: definition for definition in definitions}


def hard_limit_label(definition):
    if definition is None:
        return "N/A"
    eligible = getattr(definition, "is_eligible_for_limit_increase", None)
    if eligible is None:
        return "N/A"
    return "Yes" if not eligible else "No"


SEMANTIC_SCOPE_OVERRIDES = {
    ("vcn", "internet-gateway-count"): "PER_VCN",
    ("vcn", "nat-gateway-count"): "PER_VCN",
    ("vcn", "dhcp-option-count"): "PER_VCN",
    ("vcn", "route-table-count"): "PER_VCN",
    ("vcn", "security-list-count"): "PER_VCN",
    ("vcn", "subnet-count"): "PER_VCN",
    ("vcn", "networksecuritygroups-count"): "PER_VCN",
    ("vcn", "securityrules-per-networksecuritygroup-count"): "PER_NSG",
    ("certificates", "associations-per-certificates-resource-count"): "PER_CERT_RESOURCE",
    ("certificates", "versions-per-certificates-resource-count"): "PER_CERT_RESOURCE",
    ("certificates", "versions-scheduled-deletion-per-certificates-resource-count"): "PER_CERT_RESOURCE",
    ("batch-computing", "batch-concurrent-tasks-per-context-count"): "PER_CONTEXT",
    ("batch-computing", "batch-concurrent-tasks-per-fleet-count"): "PER_FLEET",
    ("batch-computing", "batch-fleet-per-context-count"): "PER_CONTEXT",
    ("batch-computing", "batch-tasks-per-job-count"): "PER_JOB",
    ("load-balancer", "backend-sets-per-lb-count"): "PER_LB",
    ("load-balancer", "hostnames-per-lb-count"): "PER_LB",
    ("load-balancer", "listeners-per-lb-count"): "PER_LB",
    ("load-balancer", "lb-max-rules-count"): "PER_LB",
    ("secrets", "max-versions-per-secret-count"): "PER_SECRET",
    ("secrets", "max-versions-scheduled-deletion-per-secret-count"): "PER_SECRET",
    ("identity", "statements-count"): "PER_POLICY",
    ("identity", "policy-statements-per-compartment-hierarchy"): "PER_COMPARTMENT_HIERARCHY",
    ("block-storage", "volumes-per-group"): "PER_VOLUME_GROUP",
}


def display_scope(service_name, limit_value):
    return SEMANTIC_SCOPE_OVERRIDES.get(
        (service_name.lower(), limit_value.name),
        limit_value.availability_domain or limit_value.scope_type or "GLOBAL",
    )


def synthetic_limit_values(service_name, settings):
    if service_name.lower() == "identity":
        return [
            SimpleNamespace(
                name="policy-statements-per-compartment-hierarchy",
                scope_type="GLOBAL",
                availability_domain=None,
                value=settings["IDENTITY_POLICY_STATEMENTS_PER_HIERARCHY_LIMIT"],
            )
        ]
    return []


def print_row(service_name, limit_value, usage):
    scope = display_scope(service_name, limit_value)
    quota = usage["quota"]
    effective_max = quota if quota is not None else limit_value.value
    limit_name = limit_value.name

    print(
        f"{ellipsize(service_name, 18):<18} "
        f"{ellipsize(limit_name, 48):<48} "
        f"{ellipsize(scope, 28):<28} "
        f"{format_number(limit_value.value, limit_name):>12} "
        f"{format_number(usage['used']):>12} "
        f"{percent_used(usage['used'], effective_max, limit_name):>8}"
    )


def main():
    args = parse_args()

    try:
        settings = load_script_settings(args.settings_file)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

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
    compartment_ids = active_compartment_ids(identity_client, tenancy_id)

    try:
        available_services = service_names(limits_client, tenancy_id, None)
    except Exception as exc:
        print(f"Failed to list OCI services: {exc}", file=sys.stderr)
        return 1

    if args.list_services:
        for service_name in available_services:
            print(service_name)
        return 0

    try:
        services = resolve_target_services(args, available_services)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    manual_usage_cache = {}

    print(f"Region: {config.get('region', 'unknown')}")
    print(
        f"{'SERVICE':<18} {'LIMIT':<48} {'SCOPE':<28} "
        f"{'MAX':>12} {'USED':>12} {'USED%':>8}"
    )
    print("-" * 126)

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
            limit_values.extend(synthetic_limit_values(service_name, settings))
        except Exception as exc:
            failed_services.append((service_name, str(exc)))
            continue

        if service_name not in manual_usage_cache:
            manual_usage_cache[service_name] = manual_usage_by_service(
                config,
                identity_client,
                tenancy_id,
                compartment_ids,
                service_name,
            )
        manual_usage = manual_usage_cache[service_name]

        for limit_value in sorted(
            limit_values,
            key=lambda item: (
                item.name.lower(),
                item.availability_domain or "",
                item.scope_type or "",
            ),
        ):
            definition = definitions.get(limit_value.name)
            usage_supported = bool(
                definition and getattr(definition, "is_resource_availability_supported", False)
            )

            try:
                usage = fetch_usage(
                    limits_client,
                    tenancy_id,
                    service_name,
                    limit_value,
                    manual_usage,
                    usage_supported,
                )
            except Exception:
                usage = {
                    "used": None,
                    "quota": None,
                }

            if args.only_with_usage and (usage["used"] is None or usage["used"] <= 0):
                continue

            print_row(service_name, limit_value, usage)
            printed_rows += 1

    if failed_services:
        print("\nServices that could not be queried:", file=sys.stderr)
        for service_name, error_text in failed_services:
            print(f"  {service_name}: {error_text}", file=sys.stderr)

    if printed_rows == 0:
        print(
            "No rows returned. Retry without --only-with-usage to include zero-usage rows and limits without usage data.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
