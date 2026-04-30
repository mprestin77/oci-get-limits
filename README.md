# OCI Limits and Usage

List OCI service limits and show current usage where OCI exposes it or where usage can be calculated from service APIs.

Main script:

- `get-limits-service-usage.py`

## Features

- Lists OCI limits for one service, a configured set of services, or all services
- Shows current `USED` values when available
- Adds manual usage collection for selected services where the Limits API is incomplete
- Supports service filtering through a plain-text config file
- Can export results to CSV
- Supports semantic scope labels such as `PER_VCN`, `PER_LB`, and `PER_POLICY`
- Can show only limits with non-zero usage

## Requirements

- Python 3
- OCI Python SDK
- OCI config file, by default `~/.oci/config`
- OCI permissions to read limits and relevant service resources

## IAM policy

The exact least-privilege policy depends on which services you query.

This script:

- reads OCI Limits data at the tenancy level
- lists compartments
- lists and reads resources for selected services when manual usage calculation is needed

A practical read-only policy for running the script across many services is:

```text
Allow group <group-name> to inspect compartments in tenancy
Allow group <group-name> to read all-resources in tenancy
```

If you want stricter least privilege, grant `read` access only to the specific service families used by your `services.conf` file, plus the permissions needed to view limits and compartments.

## Files

- `get-limits-service-usage.py` - main script
- `services.conf` - sample services file
- `limits-settings.conf` - script settings file

## Usage

Run for all services:

```bash
python3 get-limits-service-usage.py
```

List all OCI services:

```bash
python3 get-limits-service-usage.py --list-services
```

Run for a single service:

```bash
python3 get-limits-service-usage.py --service compute
```

Run only for services in a file:

```bash
python3 get-limits-service-usage.py --services-file services.conf
```

Run only for limits with non-zero usage:

```bash
python3 get-limits-service-usage.py --only-with-usage
```

Write output to a file:

```bash
python3 get-limits-service-usage.py --csv limits.csv
```

Run in a specific region:

```bash
python3 get-limits-service-usage.py --region us-ashburn-1
```

Run in multiple regions:

```bash
python3 get-limits-service-usage.py --region us-ashburn-1,us-phoenix-1
```

Run in all subscribed tenancy regions:

```bash
python3 get-limits-service-usage.py --region all
```

Write multi-region output to separate CSV files:

```bash
python3 get-limits-service-usage.py --csv limits.csv --region us-ashburn-1,us-phoenix-1
```

Use a specific OCI profile:

```bash
python3 get-limits-service-usage.py --profile PROD
```

Use a custom settings file:

```bash
python3 get-limits-service-usage.py --settings-file limits-settings.conf
```

## Services file format

`--services-file` expects a plain-text file with one OCI service name per line.

- blank lines are ignored
- lines starting with `#` are ignored

Example:

```text
# services.conf
identity
compute
vcn
load-balancer
faas
```

## Settings file format

`--settings-file` expects a simple `KEY = VALUE` file.

Settings from this file are used only for limits whose values are not exposed directly through the OCI Limits API.

Example:

```text
IDENTITY_DYNAMIC_GROUPS_LIMIT = 300
IDENTITY_POLICY_STATEMENTS_PER_HIERARCHY_LIMIT = 500
```

## Flags

Show all available flags and their descriptions:

```bash
python3 get-limits-service-usage.py --help
```

These options are mutually exclusive:

- `--service`
- `--services-file`
- `--list-services`

`--only-with-usage` means:

- show only rows where `USED > 0`

`--csv` means:

- single region: write one CSV file
- multiple regions: write one CSV file per region
- for multi-region runs, the script adds the region name to the filename
- if the filename does not end with `.csv`, the script writes `.csv` files

`--region all` means:

- resolve all tenancy region subscriptions with status `READY`
- run the same limits and usage collection in each of those regions

## Output

Columns:

- `SERVICE`
- `LIMIT`
- `SCOPE`
- `MAX`
- `USED`
- `USED%`

For some known limits, the script replaces OCI’s generic scope labels with semantic scopes such as:

- `PER_VCN`
- `PER_NSG`
- `PER_LB`
- `PER_SECRET`
- `PER_POLICY`
- `PER_CONTEXT`
- `PER_FLEET`
- `PER_JOB`
- `PER_VOLUME_GROUP`
- `PER_CERT_RESOURCE`
- `PER_COMPARTMENT_HIERARCHY`

## Supported manual collectors

The script adds manual usage collection for selected services, including:

- `identity`
- `certificates`
- `batch-computing`
- `block-storage`
- `vcn`
- `fast-connect`
- `dns`
- `notifications`
- `faas`
- `load-balancer`
- `secrets`
- `resource-scheduler`
- `regions`
- `container-engine`

## Identity note

The script adds synthetic Identity rows:

- `dynamic-groups-count`
- `policy-statements-per-compartment-hierarchy`

`dynamic-groups-count` is calculated by listing dynamic groups in the tenancy and comparing the result to the configured value of `IDENTITY_DYNAMIC_GROUPS_LIMIT`, which defaults to `300`.

`policy-statements-per-compartment-hierarchy` is calculated as the maximum cumulative number of policy statements along any existing compartment path, using the configured value of `IDENTITY_POLICY_STATEMENTS_PER_HIERARCHY_LIMIT`, which defaults to `500`.

## Limitations

- OCI does not expose current usage for every limit
- Some limits are rate, token, throughput, or session limits and cannot be derived safely from resource inventory
- In those cases, `USED` is shown as `N/A`
- The script prints raw numeric `MAX` values returned by OCI

## Example

```bash
python3 get-limits-service-usage.py --only-with-usage --services-file services.conf -r us-ashburn-1
```

```text
Region: us-ashburn-1
SERVICE            LIMIT                                            SCOPE                                 MAX         USED    USED%
------------------------------------------------------------------------------------------------------------------------------
identity           free-domains-count                               GLOBAL                                 10            1    10.0%
identity           policies-count                                   GLOBAL                                100           47    47.0%
identity           policy-statements-per-compartment-hierarchy      PER_COMPARTMENT_HIERARCHY             500          220    44.0%
identity           statements-count                                 PER_POLICY                             50           25    50.0%
compartments       compartment-count                                REGION                              1,000            8     0.8%
vcn                dhcp-option-count                                PER_VCN                               300            1     0.3%
vcn                drg-count                                        REGION                                  5            2    40.0%
vcn                flow-log-config-count                            GLOBAL                                100            1     1.0%
vcn                internet-gateway-count                           PER_VCN                                 1            1   100.0%
vcn                nat-gateway-count                                PER_VCN                                 1            1   100.0%
vcn                networksecuritygroups-count                      PER_VCN                             1,000            1     0.1%
vcn                reserved-public-ip-count                         REGION                                 50            2     4.0%
vcn                route-table-count                                PER_VCN                               300            5     1.7%
vcn                security-list-count                              PER_VCN                               300            6     2.0%
vcn                securityrules-per-networksecuritygroup-count     PER_NSG                               120            2     1.7%
vcn                subnet-count                                     PER_VCN                               300            5     1.7%
vcn                vcn-count                                        REGION                                 50            7    14.0%
load-balancer      backend-sets-per-lb-count                        PER_LB                                 16            1     6.2%
load-balancer      lb-100mbps-count                                 REGION                                300            3     1.0%
load-balancer      listeners-per-lb-count                           PER_LB                                 16            1     6.2%
faas               application-count                                REGION                                 20            1     5.0%
faas               function-count                                   REGION                                500            1     0.2%
compute            custom-image-count                               REGION                                500           10     2.0%
compute            dense-io-e4-core-count                           ZyrR:US-ASHBURN-AD-1                2,000            8     0.4%
compute            dense-io-e4-memory-count                         ZyrR:US-ASHBURN-AD-1               32,000          128     0.4%
compute            standard-e4-core-count                           ZyrR:US-ASHBURN-AD-1                2,000           19     0.9%
compute            standard-e4-memory-count                         ZyrR:US-ASHBURN-AD-1               32,000          195     0.6%
compute            standard-e5-core-count                           ZyrR:US-ASHBURN-AD-1                2,000           23     1.1%
compute            standard-e5-core-count                           ZyrR:US-ASHBURN-AD-2                2,000            1     0.1%
compute            standard-e5-core-count                           ZyrR:US-ASHBURN-AD-3                2,000            3     0.1%
compute            standard-e5-memory-count                         ZyrR:US-ASHBURN-AD-1               24,000          316     1.3%
compute            standard-e5-memory-count                         ZyrR:US-ASHBURN-AD-2               24,000           16     0.1%
compute            standard-e5-memory-count                         ZyrR:US-ASHBURN-AD-3               24,000           44     0.2%
compute            standard1-core-count-reservable                  ZyrR:US-ASHBURN-AD-1                2,000            1     0.1%
```
