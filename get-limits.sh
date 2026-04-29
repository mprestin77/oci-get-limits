# Set your Tenancy OCID
TENANCY_OCID=ocid1.tenancy.oc1..aaaaaaaaifplmln563scofwisfcqcqsuqyvqc5is2k7ia35227hpudxmxwgq

# Use join() in the query to get a clean, space-separated string of names
for service in $(oci limits service list --compartment-id $TENANCY_OCID --all --query "join(' ', data[].name)" --raw-output); do
    echo "=========================================================="
    echo "SERVICE: $service"
    echo "=========================================================="
    oci limits value list --compartment-id $TENANCY_OCID --service-name "$service" --all --output table
done

