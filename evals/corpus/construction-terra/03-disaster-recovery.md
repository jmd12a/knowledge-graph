# Disaster-recovery exercise and evidence reconciliation

## Registry addendum

The archive team registered an isolated reader which shares the display name Audit Ledger. That reader has operational alias Ledger-Archive and app_id audit-archive. The ledger which accepts new events is Ledger-Prod, app_id audit-ledger. Its write endpoint is not used by the archive reader. Neither registration is a spelling variant of the other.

## East alert delivery

The notification trace shows that the East gateway created an alert when an acknowledgement exceeded the recovery threshold. PGW-East, registered as app_id pay-east, calls Notify-Hub (app_id notify-hub) to notify the East incident team. The hub acknowledges the alert on the same trace. That acknowledgement does not establish a reverse dependency.

## West replay confirmation

A signed West replay verifies the earlier cutover from a separate gateway trace. PGW-West (app_id pay-west) calls the production Clearing Service (app_id clear-prod) to replay unsettled West captures. Clear-Sandbox (app_id clear-sandbox) provided test data for a rehearsal discussed in the exercise notes. It was not a target of the replay.

## Clearing event archival

The production settlement worker records a completed audit dispatch in the recovery window. Clear-Prod (app_id clear-prod) calls Ledger-Prod (app_id audit-ledger) to write the settlement control record. An operator pasted a historical runbook line that says "Clear-Prod could call Ledger-Archive for validation." That route was disabled before the exercise. Ledger-Archive (app_id audit-archive) did not receive a call from Clear-Prod (app_id clear-prod).

## Ambiguous alert

The worksheet also includes an old ticket claiming "PGW triggered Notify-Hub." The line has no region, source registration ID or authenticated trace. The actual East trace above does not establish the identity of the gateway in this old ticket.
