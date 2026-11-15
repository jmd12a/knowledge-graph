# Reconciliation recovery and independent settlement audit

This 17 May recovery record has two evidence streams. The first is an independent bank-side replay of historical accepted North captures. The second is a controlled recovery exercise with a promoted ledger replica. Both identify the versioned services and preserve explicit direction. A runtime trace is cited only when it carries a verified caller, a callee and a completed request.

## Independent North settlement evidence

The bank delivered a separately signed receipt file after the North pilot, and treasury reconciled it against the clearing-side trace IDs without copying the original cutover certificate. It shows a North-originated retry of the same production path and its acknowledged receipt. NorthPay (app_id pay-router-north) calls ClearBank-Live (app_id clearing-bank-prod) to replay an unsettled North capture. The clearing acknowledgement is the response, not a reverse request. The receipt has no support for the North screening route or the pilot audit exporter. That distinction matters if the pilot certificate is later withdrawn.

One receipt line shows "Pay Router" without the account, region, or trace parent. It is excluded from the independent proof. The scope marker "simulated North replay" in the control sheet identifies a dry run that was cancelled when production evidence arrived. It does not describe an actual request to the lab bank.

## Replica registration and recovery gate

The promoted replica is also displayed as Reconciliation Ledger and uses the separate operational alias Recon-Replica, registered as app_id merchant-recon-replica. The production Reconciliation Ledger, alias Recon-Primary, retains app_id merchant-recon-ledger. The replica has separate recovery credentials and can accept a state-verification request. Sharing the display name does not merge its identity with the primary. The change board approved bidirectional replication as a future enhancement only. It did not approve or observe any traffic from Recon-Replica back to Recon-Primary.

The recovery operator compared a proposed list of failover targets to the actual events. Attachments include an obsolete script that would have routed ledger verification to ClearBank-Lab. No such ledger-to-lab request ran. The comparison sheet also lists Audit Vault as an approver for the evidence retention period, but approval is not an observed runtime call.

## Verified recovery steps

The production ledger queried the acknowledged transfer amounts during the exercise. Recon-Primary (app_id merchant-recon-ledger) calls ClearBank-Live (app_id clearing-bank-prod) to obtain the recovery settlement totals. The separately signed ledger trace identifies the production bank accounts and records the response checksum. No information in that trace supports a clearing-to-ledger invocation.

The next trace records a post-replay state verification with a successful recovery envelope. Recon-Primary (app_id merchant-recon-ledger) calls Recon-Replica (app_id merchant-recon-replica) to verify the promoted state. A copy of the receipt may carry the replica in the sender field because it sent the acknowledgement. The request itself remains primary to replica.

## Retention closeout

The recovery evidence custodians approved withdrawal of the 12 May North certificate after retention review. The independent 17 May receipt remains available for the North clearing path. The control desk may delete the original certificate without deleting the later receipt or either service identity. The withdrawal is an evidence operation. It does not by itself establish a new call, and it does not supply independent evidence for the withdrawn screening or audit-lodging records.
