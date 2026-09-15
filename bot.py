import os
import sys
import time

import oci
from oci.exceptions import ServiceError

TARGET_SHAPE = "VM.Standard.A1.Flex"
TARGET_OCPUS = int(os.getenv("OCI_OCPUS", "2"))
TARGET_MEMORY_GB = int(os.getenv("OCI_MEMORY_GB", "12"))
BOOT_VOLUME_GB = int(os.getenv("OCI_BOOT_VOLUME_GB", "100"))
MAX_ATTEMPTS = int(os.getenv("OCI_MAX_ATTEMPTS", "3"))
RETRY_DELAY_SECONDS = int(os.getenv("OCI_RETRY_DELAY_SECONDS", "45"))
DISPLAY_NAME = os.getenv("OCI_DISPLAY_NAME", "erp-a1-flex-2ocpu-12gb")


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def get_availability_domains(identity_client, tenancy_id: str) -> list[str]:
    configured = os.getenv("OCI_AVAILABILITY_DOMAINS", "").strip()
    if configured:
        return [item.strip() for item in configured.split(",") if item.strip()]

    response = identity_client.list_availability_domains(compartment_id=tenancy_id)
    domains = [ad.name for ad in response.data]
    if not domains:
        raise RuntimeError("OCI returned no Availability Domains.")
    return domains


def find_oracle_linux_9_arm64_image(compute_client, compartment_id: str) -> str:
    """Resolve the current Oracle Linux 9 ARM64 image; do not hard-code a regional OCID."""
    response = compute_client.list_images(
        compartment_id=compartment_id,
        operating_system="Oracle Linux",
        operating_system_version="9",
        shape=TARGET_SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
        limit=20,
    )

    candidates = [
        image for image in response.data
        if (image.lifecycle_state or "").upper() == "AVAILABLE"
        and (image.architecture or "").lower() in {"aarch64", "arm64"}
    ]

    if not candidates:
        raise RuntimeError(
            "No AVAILABLE Oracle Linux 9 ARM64 image was found for "
            f"{TARGET_SHAPE} in region {os.getenv('OCI_REGION')}."
        )

    selected = candidates[0]
    print(
        f"Selected image: {selected.display_name} | "
        f"OCID: {selected.id} | architecture: {selected.architecture}"
    )
    return selected.id


def is_capacity_error(error: ServiceError) -> bool:
    message = str(error).lower()
    return (
        "out of host capacity" in message
        or "out of capacity" in message
        or error.status in {409, 500, 503}
    )


def instance_already_exists(compute_client, compartment_id: str) -> bool:
    response = compute_client.list_instances(
        compartment_id=compartment_id,
        display_name=DISPLAY_NAME,
    )
    for instance in response.data:
        if instance.lifecycle_state not in {"TERMINATED", "TERMINATING"}:
            print(
                f"Existing instance detected: {instance.display_name} "
                f"({instance.id}) state={instance.lifecycle_state}"
            )
            return True
    return False


def main() -> int:
    config = {
        "user": required("OCI_USER_ID"),
        "key_content": required("OCI_PRIVATE_KEY"),
        "fingerprint": required("OCI_FINGERPRINT"),
        "tenancy": required("OCI_TENANCY_ID"),
        "region": required("OCI_REGION"),
    }

    tenancy_id = config["tenancy"]
    compartment_id = os.getenv("OCI_COMPARTMENT_ID", tenancy_id).strip()
    subnet_id = required("OCI_SUBNET_ID")
    public_ssh_key = required("OCI_PUBLIC_SSH_KEY")

    print("=== OCI A1 Flex Provisioner ===")
    print(f"Region: {config['region']}")
    print(f"Shape: {TARGET_SHAPE}")
    print(f"Target: {TARGET_OCPUS} OCPU / {TARGET_MEMORY_GB} GB RAM")
    print("OS: Oracle Linux 9 ARM64")
    print(f"Boot volume: {BOOT_VOLUME_GB} GB")
    print(f"Display name: {DISPLAY_NAME}")

    compute_client = oci.core.ComputeClient(config)
    identity_client = oci.identity.IdentityClient(config)

    try:
        ads = get_availability_domains(identity_client, tenancy_id)
        print("Availability Domains:", ", ".join(ads))

        if instance_already_exists(compute_client, compartment_id):
            print("Nothing to do: target instance already exists.")
            return 0

        image_id = find_oracle_linux_9_arm64_image(
            compute_client, compartment_id
        )
    except ServiceError as error:
        print(f"OCI discovery failed: {error.message or error}")
        return 1

    for attempt in range(1, MAX_ATTEMPTS + 1):
        availability_domain = ads[(attempt - 1) % len(ads)]
        print(
            f"[Attempt {attempt}/{MAX_ATTEMPTS}] "
            f"Requesting {TARGET_SHAPE} in {availability_domain}"
        )

        launch_details = oci.core.models.LaunchInstanceDetails(
            display_name=DISPLAY_NAME,
            compartment_id=compartment_id,
            availability_domain=availability_domain,
            shape=TARGET_SHAPE,
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=TARGET_OCPUS,
                memory_in_gbs=TARGET_MEMORY_GB,
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                source_type="image",
                image_id=image_id,
                boot_volume_size_in_gbs=BOOT_VOLUME_GB,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True,
                assign_private_dns_record=True,
            ),
            metadata={"ssh_authorized_keys": public_ssh_key},
        )

        try:
            response = compute_client.launch_instance(launch_details)
            print(
                "SUCCESS: OCI instance launch accepted. "
                f"instance_id={response.data.id} "
                f"lifecycle_state={response.data.lifecycle_state}"
            )
            return 0
        except ServiceError as error:
            if is_capacity_error(error):
                print(
                    "Capacity unavailable for this attempt: "
                    f"{error.message or str(error)}"
                )
            else:
                print(
                    "Non-capacity OCI error: "
                    f"status={error.status}; message={error.message or str(error)}"
                )
                return 1

        if attempt < MAX_ATTEMPTS:
            print(f"Waiting {RETRY_DELAY_SECONDS}s before next attempt...")
            time.sleep(RETRY_DELAY_SECONDS)

    print("No instance created in this run. The next scheduled run will retry.")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"FATAL: {exc}")
        sys.exit(1)
