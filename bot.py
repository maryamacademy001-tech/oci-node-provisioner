import os
import random
import sys
import time

import oci
from oci.exceptions import ServiceError

TARGET_SHAPE = "VM.Standard.A1.Flex"
TARGET_OCPUS = int(os.getenv("OCI_OCPUS", "2"))
TARGET_MEMORY_GB = int(os.getenv("OCI_MEMORY_GB", "12"))
BOOT_VOLUME_GB = int(os.getenv("OCI_BOOT_VOLUME_GB", "100"))
DISPLAY_NAME = os.getenv("OCI_DISPLAY_NAME", "erp-a1-flex-2ocpu-12gb")

# --- Retry governance -------------------------------------------------------
# The GitHub Actions job has timeout-minutes: 5 (300s). MAX_RUNTIME_SECONDS is a
# self-imposed budget, measured from process start, that must stay below that so
# the script always exits cleanly (code 2) instead of being killed mid-sleep.
# The hourly scheduler is the primary capacity-retry mechanism; in-run retries
# are only a bounded supplement and must never overrun the job window.
MAX_ATTEMPTS = int(os.getenv("OCI_MAX_ATTEMPTS", "3"))
MAX_RUNTIME_SECONDS = int(os.getenv("OCI_MAX_RUNTIME_SECONDS", "210"))
RUN_RESERVE_SECONDS = int(os.getenv("OCI_RUN_RESERVE_SECONDS", "20"))

# Per-category base delays. Actual delay = base * 2**(occurrence-1) + jitter,
# i.e. exponential backoff with a random component to avoid a retry storm when
# multiple scheduled runs overlap. Capacity gets the longest base because A1
# Flex host availability frees on the order of minutes, not seconds.
CAPACITY_DELAY_SECONDS = int(os.getenv("OCI_CAPACITY_DELAY_SECONDS", "120"))
THROTTLE_DELAY_SECONDS = int(os.getenv("OCI_THROTTLE_DELAY_SECONDS", "60"))
TRANSIENT_DELAY_SECONDS = int(os.getenv("OCI_TRANSIENT_DELAY_SECONDS", "30"))
RETRY_JITTER_SECONDS = int(os.getenv("OCI_RETRY_JITTER_SECONDS", "30"))

# Explicit error categories. Every ServiceError maps to exactly one of these;
# no error is silently lumped into a broad bucket.
CAPACITY = "CAPACITY"
THROTTLED = "THROTTLED"
TRANSIENT = "TRANSIENT"
NON_RETRYABLE = "NON-RETRYABLE"


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def build_config() -> dict:
    return {
        "user": required("OCI_USER_ID"),
        "key_content": required("OCI_PRIVATE_KEY"),
        "fingerprint": required("OCI_FINGERPRINT"),
        "tenancy": required("OCI_TENANCY_ID"),
        "region": required("OCI_REGION"),
    }


def preflight(config: dict) -> None:
    """Report the shape of the OCI auth inputs without ever printing secret values."""
    print("=== OCI Authentication Preflight ===")
    print(f"OCI SDK version: {oci.__version__}")

    key = config["key_content"]
    header_ok = "-----BEGIN PRIVATE KEY-----" in key
    footer_ok = "-----END PRIVATE KEY-----" in key
    literal_bs_n = "\\n" in key
    key_stripped = key.strip()
    has_surrounding_ws = key_stripped != key or key != key_stripped
    print(
        "OCI_PRIVATE_KEY: present=YES length={0} pem_header={1} pem_footer={2} "
        "newline_count={3} literal_\\n={4} surrounding_whitespace={5}".format(
            len(key), header_ok, footer_ok, key.count("\n"), literal_bs_n,
            has_surrounding_ws,
        )
    )
    print(
        "OCI_USER_ID: present=YES length={0} looks_like_ocid={1}".format(
            len(config["user"]), config["user"].startswith("ocid1.user.oc1.")
        )
    )
    print(
        "OCI_TENANCY_ID: present=YES length={0} looks_like_ocid={1}".format(
            len(config["tenancy"]), config["tenancy"].startswith("ocid1.tenancy.oc1.")
        )
    )
    print(
        "OCI_FINGERPRINT: present=YES length={0} colon_hex_format={1}".format(
            len(config["fingerprint"]),
            len(config["fingerprint"].split(":")) == 20
            and all(len(part) == 2 for part in config["fingerprint"].split(":")),
        )
    )
    print(
        "OCI_REGION: present=YES value_len={0} region={1}".format(
            len(config["region"]), config["region"]
        )
    )

    try:
        oci.config.validate_config(config)
        print("oci.config.validate_config: PASS")
    except Exception as exc:
        print(f"oci.config.validate_config: FAIL ({exc})")


def describe_service_error(error: ServiceError) -> str:
    """Return a safe, diagnostic one-line summary of a ServiceError."""
    parts = [
        f"status={getattr(error, 'status', '?')}",
        f"code={getattr(error, 'code', '?')}",
    ]
    request_id = getattr(error, "request_id", None)
    if request_id:
        parts.append(f"request_id={request_id}")
    message = getattr(error, "message", None)
    if message:
        parts.append(f"message={message}")
    return " ".join(parts)


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
    """Resolve the current Oracle Linux 9 ARM64 image; do not hard-code a regional OCID.

    The VM.Standard.A1.Flex shape is ARM64-only, so passing shape=TARGET_SHAPE
    to list_images() already restricts results to ARM64-compatible images.
    The Image model in oci==2.186.0 has no 'architecture' attribute, so
    architecture filtering must rely on shape-based filtering rather than an
    Image.architecture field check.
    """
    response = compute_client.list_images(
        compartment_id=compartment_id,
        operating_system="Oracle Linux",
        operating_system_version="9",
        shape=TARGET_SHAPE,
        lifecycle_state="AVAILABLE",
        sort_by="TIMECREATED",
        sort_order="DESC",
        limit=20,
    )

    candidates = [
        image for image in response.data
        if (image.lifecycle_state or "").upper() == "AVAILABLE"
    ]

    if not candidates:
        raise RuntimeError(
            "No AVAILABLE Oracle Linux 9 image compatible with "
            f"{TARGET_SHAPE} was found in region {os.getenv('OCI_REGION')}. "
            "The 'image.architecture' field is not exposed by oci==2.186.0; "
            "ARM64 compatibility is determined by the shape parameter passed "
            "to list_images(), which restricts results to images compatible "
            "with VM.Standard.A1.Flex (an ARM64-only shape)."
        )

    selected = candidates[0]
    print(
        f"Selected image: {selected.display_name} | "
        f"OCID: {selected.id} | "
        f"operating_system: {selected.operating_system} | "
        f"operating_system_version: {selected.operating_system_version} | "
        f"lifecycle_state: {selected.lifecycle_state} | "
        f"launch_mode: {selected.launch_mode}"
    )
    return selected.id


def classify_error(error: ServiceError) -> str:
    """Map an OCI ServiceError to exactly one explicit retry category.

    Classification is deliberately ordered and evidence-based: it inspects the
    HTTP status, the OCI error code, and the error message text. It never treats
    a whole status code family as one condition, so a generic 500 is NOT assumed
    to be capacity, and a 429 is NOT treated as a permanent failure.
    """
    status = getattr(error, "status", None)
    code = str(getattr(error, "code", "") or "").lower()
    message = str(getattr(error, "message", "") or "").lower()
    # str(error) includes status/code/message, so it is a superset catch-all for
    # message text regardless of how the SDK populated the fields.
    blob = str(error).lower()

    # 1) Throttling: explicit rate limiting. Always retryable, never permanent.
    if (
        status == 429
        or "toomanyrequests" in code
        or "too many requests" in message
        or "rate limit" in message
    ):
        return THROTTLED

    # 2) Capacity: A1 Flex host/AD availability. Retryable with long backoff.
    if (
        "out of host capacity" in message
        or "out of capacity" in message
        or "out of host capacity" in blob
        or "out of capacity" in blob
        or "limitexceeded" in code
    ):
        return CAPACITY

    # 3) Known-safe transient service conditions only. Conservative retry.
    if status in {503} or "serviceunavailable" in code or "service unavailable" in message:
        return TRANSIENT

    # 4) Everything else: 400/401/403/404/409, genuine 500 with no capacity
    #    text, invalid shape/image/subnet/parameter. Fail immediately.
    return NON_RETRYABLE


def retry_delay(category: str, occurrence: int) -> float:
    """Compute the delay (seconds) before the next attempt for a category.

    Exponential backoff (base * 2**(n-1)) plus bounded random jitter so that
    concurrent or overlapping runs do not retry in lock-step.
    """
    base = {
        CAPACITY: CAPACITY_DELAY_SECONDS,
        THROTTLED: THROTTLE_DELAY_SECONDS,
        TRANSIENT: TRANSIENT_DELAY_SECONDS,
    }.get(category, 0)
    if base <= 0:
        return 0.0
    backoff = base * (2 ** (occurrence - 1))
    jitter = random.uniform(0.0, float(RETRY_JITTER_SECONDS))
    return backoff + jitter


def find_existing_instance(compute_client, compartment_id: str):
    """Return the live instance matching DISPLAY_NAME, or None if absent.

    TERMINATED/TERMINATING instances are ignored, so a previously deleted
    instance does not block re-creation.
    """
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
            return instance
    return None


def report_run_context() -> None:
    """Identify the run: trigger type, workflow run id, and target parameters.

    Uses only GitHub Actions default context variables and target config. No
    secret values are read or printed.
    """
    event = os.getenv("GITHUB_EVENT_NAME", "local")
    if event == "schedule":
        trigger = "scheduled"
    elif event == "workflow_dispatch":
        trigger = "manual (workflow_dispatch)"
    else:
        trigger = event

    print("=== Run Context ===")
    print(f"trigger: {trigger}")
    print(f"workflow_run_id: {os.getenv('GITHUB_RUN_ID', 'n/a')}")
    print(f"run_attempt: {os.getenv('GITHUB_RUN_ATTEMPT', 'n/a')}")
    print(f"target_display_name: {DISPLAY_NAME}")
    print(f"target_region: {os.getenv('OCI_REGION', 'n/a')}")
    print(f"target_shape: {TARGET_SHAPE}")
    print(f"target_ocpus: {TARGET_OCPUS}")
    print(f"target_memory_gb: {TARGET_MEMORY_GB}")


def report_instance(instance) -> None:
    """Print a safe, best-effort summary of a created or existing instance.

    Only reads attributes that are already present on the object; missing
    attributes degrade to 'n/a' and never raise, so reporting can never turn a
    successful launch into a failed run.
    """

    def attr(name):
        return getattr(instance, name, None) or "n/a"

    shape_config = getattr(instance, "shape_config", None)
    print("=== Instance Summary ===")
    print(f"display_name: {attr('display_name')}")
    print(f"ocid: {attr('id')}")
    print(f"lifecycle_state: {attr('lifecycle_state')}")
    print(f"availability_domain: {attr('availability_domain')}")
    print(f"shape: {attr('shape')}")
    if shape_config is not None:
        print(f"ocpus: {getattr(shape_config, 'ocpus', 'n/a')}")
        print(f"memory_in_gbs: {getattr(shape_config, 'memory_in_gbs', 'n/a')}")


def main() -> int:
    config = build_config()

    tenancy_id = config["tenancy"]
    compartment_id = os.getenv("OCI_COMPARTMENT_ID", tenancy_id).strip()
    subnet_id = required("OCI_SUBNET_ID")
    public_ssh_key = required("OCI_PUBLIC_SSH_KEY")

    print("=== OCI A1 Flex Provisioner ===")
    report_run_context()
    print(f"Region: {config['region']}")
    print(f"Shape: {TARGET_SHAPE}")
    print(f"Target: {TARGET_OCPUS} OCPU / {TARGET_MEMORY_GB} GB RAM")
    print("OS: Oracle Linux 9 ARM64")
    print(f"Boot volume: {BOOT_VOLUME_GB} GB")
    print(f"Display name: {DISPLAY_NAME}")

    preflight(config)

    compute_client = oci.core.ComputeClient(config)
    identity_client = oci.identity.IdentityClient(config)

    try:
        ads = get_availability_domains(identity_client, tenancy_id)
        print("Availability Domains:", ", ".join(ads))

        image_id = find_oracle_linux_9_arm64_image(
            compute_client, compartment_id
        )
    except ServiceError as error:
        category = classify_error(error)
        print(f"{category}: OCI discovery failed: {describe_service_error(error)}")
        # Discovery throttling/403s are transient and safe for the scheduler to
        # retry; only hard configuration failures return 1.
        return 2 if category in {THROTTLED, TRANSIENT} else 1

    started = time.monotonic()

    def remaining_budget() -> float:
        return MAX_RUNTIME_SECONDS - (time.monotonic() - started)

    occurrence = {CAPACITY: 0, THROTTLED: 0, TRANSIENT: 0}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Never overrun the Actions job window. If the budget is spent, stop and
        # let the next scheduled run retry, rather than being killed mid-flight.
        if remaining_budget() <= RUN_RESERVE_SECONDS and attempt > 1:
            print(
                "BUDGET: run-time budget exhausted; stopping in-run retries. "
                "The next scheduled run will retry."
            )
            break

        availability_domain = ads[(attempt - 1) % len(ads)]
        print(
            f"[Attempt {attempt}/{MAX_ATTEMPTS}] "
            f"Requesting {TARGET_SHAPE} in {availability_domain}"
        )

        existing = find_existing_instance(compute_client, compartment_id)
        if existing is not None:
            report_instance(existing)
            print("RESULT: SUCCESS - Instance already exists.")
            return 0

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
            print("Launch accepted by OCI.")
            report_instance(response.data)
            print("RESULT: SUCCESS - Instance created successfully.")
            return 0
        except ServiceError as error:
            category = classify_error(error)
            summary = describe_service_error(error)

            if category == NON_RETRYABLE:
                print(f"NON-RETRYABLE: {summary}")
                print("RESULT: CONFIGURATION_ERROR - Human intervention required.")
                return 1

            occurrence[category] += 1
            if category == CAPACITY:
                print(f"CAPACITY: {summary} Retrying with long backoff...")
            elif category == THROTTLED:
                print(f"THROTTLED: {summary} Backing off...")
            else:
                print(f"TRANSIENT: {summary} Conservative retry...")

        # Only sleep if another attempt remains AND the delay fits the budget.
        if attempt < MAX_ATTEMPTS:
            delay = retry_delay(category, occurrence[category])
            if delay >= remaining_budget() - RUN_RESERVE_SECONDS:
                print(
                    "BUDGET: next backoff would exceed the run-time budget; "
                    "deferring to the next scheduled run."
                )
                break
            print(f"Waiting {delay:.0f}s before next attempt...")
            time.sleep(delay)

    print("RESULT: CAPACITY_UNAVAILABLE - No instance created in this run.")
    print("The next scheduled run will retry.")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"FATAL: {exc}")
        sys.exit(1)
