cat << 'EOF' > get-limits2.py
import oci
import argparse
import sys

# Load default config from ~/.oci/config
config = oci.config.from_file()
tenancy_id = config["tenancy"]

def get_manual_identity_usage(identity_client):
    """Manual count for Identity resources (Global)."""
    compartments = oci.pagination.list_call_get_all_results(
        identity_client.list_compartments, tenancy_id, 
        compartment_id_in_subtree=True, access_level="ACCESSIBLE"
    ).data
    comp_ids = [tenancy_id] + [c.id for c in compartments if c.lifecycle_state == "ACTIVE"]

    policy_count = 0
    statement_count = 0
    for cid in comp_ids:
        try:
            policies = identity_client.list_policies(cid).data
            policy_count += len(policies)
            statement_count += sum(len(p.statements) for p in policies)
        except: 
            continue

    users = oci.pagination.list_call_get_all_results(identity_client.list_users, tenancy_id).data
    groups = oci.pagination.list_call_get_all_results(identity_client.list_groups, tenancy_id).data
    
    return {
        "policies-count": policy_count, 
        "statements-count": statement_count,
        "user-count": len(users), 
        "group-count": len(groups)
    }

def get_limits(limits_client, identity_client, service_name, show_usage=False):
    """Fetches limits and handles regional/AD-scoped usage."""
    try:
        limit_values = oci.pagination.list_call_get_all_results(
            limits_client.list_limit_values,
            compartment_id=tenancy_id,
            service_name=service_name
        ).data

        manual_id_counts = {}
        if show_usage and service_name.lower() == "identity":
            manual_id_counts = get_manual_identity_usage(identity_client)

        for limit in limit_values:
            usage_str = "N/A"
            scope = limit.availability_domain if limit.availability_domain else "GLOBAL"
            
            if show_usage:
                if service_name.lower() == "identity" and limit.name in manual_id_counts:
                    usage_str = str(manual_id_counts[limit.name])
                else:
                    try:
                        avail = limits_client.get_resource_availability(
                            service_name=service_name, 
                            limit_name=limit.name, 
                            compartment_id=tenancy_id,
                            availability_domain=limit.availability_domain
                        ).data
                        usage_str = str(avail.used) if avail.used is not None else "0"
                    except: 
                        usage_str = "Err"

            # FULL FORMATTED PRINT STATEMENT
            print(f"{service_name:.}")
        sys.exit(1)

    headers = f"{'Service':<15} | {'Limit Name':<40} | {'Limit':<10} | {'Usage':<10} | {'Scope'}"
    print(f"Region: {config.get('region', 'default')}")
    print(headers)
    print("-" * len(headers))

    if args.all:
        services = oci.pagination.list_call_get_all_results(limits_client.list_services, tenancy_id).data
        for s in services:
            get_limits(limits_client, identity_client, s.name, show_usage=False)
    else:
        get_limits(limits_client, identity_client, args.service, show_usage=args.usage)

if __name__ == "__main__":
    main()
EOF

