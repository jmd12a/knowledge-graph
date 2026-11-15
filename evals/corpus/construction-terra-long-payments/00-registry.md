# Bayline merchant settlement rollout — service identity register

Register release 14, signed by platform inventory and cleared for the 8–17 May rollout window. This sheet identifies independently deployable services. The ownership, credential boundaries, review assignments, and possible integrations shown here are administrative facts. The register is not a deployment record or a network trace. The change board uses an attested traffic report to determine whether an integration ran.

## Regional processing accounts

The North production instance of Payment Router is registered as app_id pay-router-north. Its trace alias is NorthPay and its service-account namespace is merchant-north. The South production instance is also displayed as Payment Router, but its registration is app_id pay-router-south and its trace alias is SouthPay. The regional desks both shorten their own instance to Pay Router. The shared display name, the common on-call rotation, and that abbreviation do not establish identity equivalence or traffic between NorthPay and SouthPay.

Each routing account authenticates a different merchant queue. Chargeback paperwork may put both router IDs on one line because two approvers sign it. That line remains an ownership record. Local dashboards drop the region from the title to fit a narrow column, so the incident desk must preserve the app_id if it opens an issue. The same display label on two tickets does not authorize a merge of the entities.

## Settlement and risk control accounts

The active production Bank Clearing service is registered as app_id clearing-bank-prod and uses the operational alias ClearBank-Live. The isolated rehearsal instance shares the display name Bank Clearing, but uses app_id clearing-bank-lab and the alias ClearBank-Lab. Laboratory credentials cannot sign production settlement batches. A validation plan may list both clearing identities because a reviewer compares the manifests. The listed pair is not a runtime connection.

The active merchant screening service named Risk Gate is registered as app_id merchant-risk-v2 and uses the trace alias RiskGate-Current. The earlier service with the same display name is separately registered as app_id merchant-risk-v1 and uses the trace alias RiskGate-Legacy. It remains inventory-visible through the archive freeze. Archived allow-list examples label that instance to explain old policy. Allowed targets, capitalized display names, and historical configuration examples establish no service calls.

## Audit and reconciliation support accounts

Audit Vault is registered as app_id merchant-audit-vault with the alias Vault-Write. Operations Notice Hub is registered as app_id merchant-notice-hub with the alias NoticeDesk. Reconciliation Ledger is registered as app_id merchant-recon-ledger with the alias Recon-Primary. These service IDs remain distinct from the people who own their access. Joint approval for a merchant exception can show all three names in a checklist even when no software integration has been enabled.

Special access expires at the end of the reported phase. An access expiry is a policy fact: it establishes neither that a call happened nor that another service is the successor. Use only a verified deployment or accepted transaction trace to record an actual CALLS edge. The inventory has no live invocation measurements.
