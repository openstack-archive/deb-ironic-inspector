#!/bin/bash

set -ex

# NOTE(vsaienko) this script is launched with sudo.
# Only exported variables are passed here.
# Source to make sure all vars are available.
STACK_ROOT="$(dirname "$0")/../../"
source "$STACK_ROOT/devstack/stackrc"
source "$STACK_ROOT/ironic/devstack/lib/ironic"

set -u

INTROSPECTION_SLEEP=${INTROSPECTION_SLEEP:-30}
export IRONIC_API_VERSION=${IRONIC_API_VERSION:-latest}
# Copied from devstack
PRIVATE_NETWORK_NAME=${PRIVATE_NETWORK_NAME:-"private"}

rules_file=$(mktemp)
cat > "$rules_file" << EOM
[
    {
        "description": "Successful Rule",
        "conditions": [
            {"op": "ge", "field": "memory_mb", "value": 256},
            {"op": "ge", "field": "local_gb", "value": 1}
        ],
        "actions": [
            {"action": "set-attribute", "path": "/extra/rule_success",
             "value": "yes"}
        ]
    },
    {
        "description": "Failing Rule",
        "conditions": [
            {"op": "lt", "field": "memory_mb", "value": 42},
            {"op": "eq", "field": "local_gb", "value": 0}
        ],
        "actions": [
            {"action": "set-attribute", "path": "/extra/rule_success",
             "value": "no"},
            {"action": "fail", "message": "This rule should not have run"}
        ]
    }
]
EOM

expected_cpus=$(openstack flavor show baremetal -f value -c vcpus)
expected_memory_mb=$(openstack flavor show baremetal -f value -c ram)
expected_cpu_arch=$(openstack flavor show baremetal -f value -c properties | sed "s/.*cpu_arch='\([^']*\)'.*/\1/")

disk_size=$(openstack flavor show baremetal -f value -c disk)
ephemeral_size=$(openstack flavor show baremetal -f value -c "OS-FLV-EXT-DATA:ephemeral")
expected_local_gb=$(($disk_size + $ephemeral_size))

ironic_url=$(openstack endpoint show baremetal -f value -c publicurl)
if [ -z "$ironic_url" ]; then
    echo "Cannot find Ironic URL"
    exit 1
fi

# NOTE(dtantsur): it's hard to get JSON field from Ironic client output, using
# HTTP API and JQ instead.

function curl_ir {
    local token=$(openstack token issue -f value -c id)
    curl -H "X-Auth-Token: $token" -X $1 "$ironic_url/$2"
}

function curl_ins {
    local token=$(openstack token issue -f value -c id)
    local args=${3:-}
    curl -f -H "X-Auth-Token: $token" -X $1 $args "http://127.0.0.1:5050/$2"
}

nodes=$(openstack baremetal node list -f value -c UUID)
if [ -z "$nodes" ]; then
    echo "No nodes found in Ironic"
    exit 1
fi

for uuid in $nodes; do
    for p in cpus cpu_arch memory_mb local_gb; do
        openstack baremetal node unset --property $p $uuid > /dev/null || true
    done
    if [[ "$(openstack baremetal node show $uuid -f value -c provision_state)" != "manageable" ]]; then
        openstack baremetal node manage $uuid
    fi
done

openstack baremetal introspection rule purge
openstack baremetal introspection rule import "$rules_file"

for uuid in $nodes; do
    openstack baremetal node inspect $uuid
done

current_nodes=$nodes
temp_nodes=
while true; do
    sleep $INTROSPECTION_SLEEP
    for uuid in $current_nodes; do
        finished=$(openstack baremetal introspection status $uuid -f value -c finished)
        if [ "$finished" = "True" ]; then
            error=$(openstack baremetal introspection status $uuid -f value -c error)
            if [ "$error" != "None" ]; then
                echo "Introspection for $uuid failed: $error"
                exit 1
            fi
        else
            temp_nodes="$temp_nodes $uuid"
        fi
    done
    if [ "$temp_nodes" = "" ]; then
        echo "Introspection done"
        break
    else
        current_nodes=$temp_nodes
        temp_nodes=
    fi
done

openstack baremetal introspection rule purge

function test_swift {
    # Basic sanity check of the data stored in Swift
    stored_data_json=$(openstack baremetal introspection data save $uuid)
    stored_cpu_arch=$(echo $stored_data_json | jq -r '.cpu_arch')
    echo CPU arch for $uuid from stored data: $stored_cpu_arch
    if [ "$stored_cpu_arch" != "$expected_cpu_arch" ]; then
        echo "The data stored in Swift does not match the expected data."
        exit 1
    fi
}

function wait_for_provision_state {
    local uuid=$1
    local expected=$2
    local max_attempts=${3:-6}

    for attempt in $(seq 1 $max_attempts); do
        local current=$(openstack baremetal node show $uuid -f value -c provision_state)

        if [ "$current" != "$expected" ]; then
            if [ "$attempt" -eq "$max_attempts" ]; then
                echo "Expected provision_state $expected, got $current:"
                openstack baremetal node show $uuid
                exit 1
            fi
        else
            break
        fi
        sleep 10
    done
}

for uuid in $nodes; do
    node_json=$(curl_ir GET v1/nodes/$uuid)
    properties=$(echo $node_json | jq '.properties')

    echo Properties for $uuid: $properties
    if [ "$(echo $properties | jq -r '.cpu_arch')" != "$expected_cpu_arch" ]; then
        echo "Expected CPU architecture: $expected_cpu_arch"
        exit 1
    fi
    if [ "$(echo $properties | jq -r '.cpus')" != "$expected_cpus" ]; then
        echo "Expected number of CPUS: $expected_cpus"
        exit 1
    fi
    if [ "$(echo $properties | jq -r '.local_gb')" != "$expected_local_gb" ]; then
        echo "Expected disk: $expected_local_gb"
        exit 1
    fi
    if [ "$(echo $properties | jq -r '.memory_mb')" != "$expected_memory_mb" ]; then
        echo "Expected memory: $expected_memory_mb"
        exit 1
    fi

    extra=$(echo $node_json | jq '.extra')
    echo Extra properties for $uuid: $extra
    if [ "$(echo $extra | jq -r '.rule_success')" != "yes" ]; then
        echo "Rule matching failed"
        exit 1
    fi

    openstack service list | grep swift && test_swift

    wait_for_provision_state $uuid manageable
    openstack baremetal node provide $uuid
done

# Cleaning kicks in here, we have to wait until it finishes (~ 2 minutes)
for uuid in $nodes; do
    wait_for_provision_state $uuid available 60  # 10 minutes for cleaning
done

echo "Wait until nova becomes aware of bare metal instances"

for attempt in {1..24}; do
    if [ $(openstack hypervisor stats show -f value -c vcpus) -ge $expected_cpus ]; then
        break
    elif [ "$attempt" -eq 24 ]; then
        echo "Timeout while waiting for nova hypervisor-stats, current:"
        openstack hypervisor stats show
        exit 1
    fi
    sleep 5
done

echo "Try nova boot for one instance"

image=$(openstack image list --property disk_format=ami -f value -c ID | head -n1)
net_id=$(openstack network show "$PRIVATE_NETWORK_NAME" -f value -c id)
# TODO(vsaienko) replace by openstack create with --wait flag
uuid=$(nova boot --flavor baremetal --nic net-id=$net_id --image $image testing | grep " id " | awk '{ print $4 }')

for attempt in {1..30}; do
    status=$(nova show $uuid | grep " status " | awk '{ print $4 }')
    if [ "$status" = "ERROR" ]; then
        echo "Instance failed to boot"
        # Some debug output
        openstack server show $uuid
        openstack hypervisor stats show
        exit 1
    elif [ "$status" != "ACTIVE" ]; then
        if [ "$attempt" -eq 30 ]; then
            echo "Instance didn't become ACTIVE, status is $status"
            exit 1
        fi
    else
        break
    fi
    sleep 30
done

openstack server delete $uuid

echo "Validation passed"
