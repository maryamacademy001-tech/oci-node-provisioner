# OCI A1 Flex Provisioner — ERP Target

Professional GitHub Actions provisioner for an Oracle Cloud Infrastructure
Ampere A1 Flex instance sized for the initial ERP deployment.

## Target

| Setting | Target |
|---|---|
| Shape | `VM.Standard.A1.Flex` |
| CPU | **2 OCPU** |
| Memory | **12 GB** |
| Architecture | **ARM64 / AArch64** |
| OS | **Oracle Linux 9** |
| Boot volume | **100 GB** |
| Public IP | Yes |
| Provisioning | GitHub Actions + OCI Python SDK |

This intentionally downsizes the previous 4 OCPU / 24 GB configuration.
For the initial 1–2 school ERP pilot, 2 OCPU / 12 GB is the requested target.

## Capacity Behavior

The workflow does **not** create a smaller Micro VM as a fallback.

If A1 Flex capacity is unavailable, it:
1. Tries configured/discovered Availability Domains.
2. Retries only a few times in the current workflow.
3. Exits without silently changing the requested shape.
4. Lets the next scheduled GitHub Actions run try again.

This prevents accidental provisioning of the wrong instance size.

## Oracle Linux 9 ARM64

The image OCID is **not hard-coded**.

The provisioner dynamically searches the selected region for an available:
- Oracle Linux
- Version 9
- AArch64/ARM64
- Compatible with `VM.Standard.A1.Flex`

This avoids relying on a Phoenix-specific image OCID.

## GitHub Actions Secrets

Required:
```text
OCI_USER_ID
OCI_PRIVATE_KEY
OCI_FINGERPRINT
OCI_TENANCY_ID
OCI_REGION
OCI_SUBNET_ID
OCI_PUBLIC_SSH_KEY
```

Optional:
```text
OCI_COMPARTMENT_ID
OCI_AVAILABILITY_DOMAINS
```

If `OCI_AVAILABILITY_DOMAINS` is omitted, the script discovers them through OCI.

Never commit private keys, API tokens, passwords, or `.env` files.

## ERP Deployment Target

```text
Cloudflare
    |
    v
HTTPS / ERP domain
    |
    v
OCI VM.Standard.A1.Flex
2 OCPU / 12 GB RAM
Oracle Linux 9 ARM64
    |
    +-- Docker
    +-- FastAPI
    +-- PostgreSQL
    +-- Redis
    +-- Celery
    +-- Nginx / reverse proxy
    |
    +-- Cloudflare R2 for media/PDFs
```

Do not expose PostgreSQL or Redis directly to the Internet.

## Post-Provisioning Checklist

- [ ] Verify ARM64 architecture.
- [ ] Verify Oracle Linux 9.
- [ ] Verify 2 OCPU / 12 GB RAM.
- [ ] Verify public IP.
- [ ] Verify SSH using key authentication.
- [ ] Configure OCI Security Group/List.
- [ ] Install Docker.
- [ ] Deploy ERP Compose stack.
- [ ] Configure persistent PostgreSQL storage.
- [ ] Configure backups.
- [ ] Configure Cloudflare HTTPS/DNS.
- [ ] Run ERP health checks.
- [ ] Perform smoke tests.
- [ ] Keep local Docker environment as rollback until production is verified.

## Engineering Principle

The provisioner has one explicit objective:

> **Obtain the requested 2 OCPU / 12 GB A1 Flex instance. Never silently substitute a 1 GB Micro VM.**

Production ERP deployment is a separate stage after the VM is verified.
