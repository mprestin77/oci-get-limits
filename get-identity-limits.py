import oci

config = oci.config.from_file()
tenancy_id = config["tenancy"]
identity = oci.identity.IdentityClient(config)
limits_client = oci.limits.LimitsClient(config)

def get_comprehensive_manual_counts():
    """Manually counts advanced identity resources and maps them to OCI limit names."""
    
    # 1. Count Network Sources
    network_sources = oci.pagination.list_call_get_all_results(
        identity.list_network_sources, tenancy_id
    ).data
    
    # 2. Detailed Domain Counting (By License Type)
    domains = oci.pagination.list_call_get_all_results(
        identity.list_domains, tenancy_id
    ).data
    
    domain_usage = {
        "free-domains-count": 0,
        "premium-domains-count": 0,
        "oracle-apps-domains-count": 0,
        "oracle-apps-premium-domains-count": 0,
        "external-user-domains-count": 0
    }

    for d in domains:
        lt = d.license_type.upper()
        if lt == "FREE": domain_usage["free-domains-count"] += 1
        elif lt == "PREMIUM": domain_usage["premium-domains-count"] += 1
        elif lt == "ORACLE_APPS": domain_usage["oracle-apps-domains-count"] += 1
        elif lt == "ORACLE_APPS_PREMIUM": domain_usage["oracle-apps-premium-domains-count"] += 1
        elif lt == "EXTERNAL_USER": domain_usage["external-user-domains-count"] += 1

    # 3. Policy and Statement Counting
    compartments = oci.pagination.list_call_get_all_results(
        identity.list_compartments, tenancy_id, 
        compartment_id_in_subtree=True, access_level="ACCESSIBLE"
    ).data
    comp_ids = [tenancy_id] + [c.id for c in compartments if c.lifecycle_state == "ACTIVE"]

    policy_count = 0
    statement_count = 0
    for cid in comp_ids:
        try:
            policies = identity.list_policies(cid).data
            policy_count += len(policies)
            statement_count += sum(len(p.statements) for p in policies)
        except Exception: continue

    return {
        "network-sources-count": len(network_sources),
        "policies-count": policy_count,
        "statements-count": statement_count,
        **domain_usage
    }

def list_final_identity_usage():
    print(f"{'Limit Name':<35} | {'Limit Value':<12} | {'Actual Usage':<12}")
    print("-" * 65)

    limit_values = oci.pagination.list_call_get_all_results(
        limits_client.list_limit_values, tenancy_id, service_name="identity"
    ).data

    usage_map = get_comprehensive_manual_counts()

    for limit in limit_values:
        # Get usage from map, default to N/A if limit is truly unknown/untracked
        actual_usage = usage_map.get(limit.name, "N/A")
        print(f"{limit.name:<35} | {limit.value:<12} | {actual_usage:<12}")

if __name__ == "__main__":
    list_final_identity_usage()

