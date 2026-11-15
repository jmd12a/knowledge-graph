# Northstar recovery replay and archive evidence

## Recovery source and accepted scope

This signed post-exercise report combines a live West evidence check with an isolated diagnostic replay for East. It is a fourth import after the inventory, the East cutover and the West verification report. The report distinguishes source event time from observation time. Successful steps below are supported by signed workload traces, while rejected designs, sample identities and human-only steps are marked in their own paragraphs. The replay control window is a limited operating exception, and its evidence does not broaden the everyday production scope.

## New archive identity

The exercise introduced the Replay Vault, registered as app_id replay-vault and marked with operational alias Vault-Manifest. Its service account is separate from Ledger-Dispatch. Replay Vault retains replay packages and manifests. Incident Ledger retains incident decisions. Operators manually synchronize their ticket references after an exercise and the use of the same ticket identifier is not a service-to-service call. A diagram with the word ledger in an archive box describes retained data in this exercise and is not another registration for Incident Ledger.

## West production recovery evidence

The control reviewer sampled a successful recovery dispatch from West and checked both workload identities against the trace registry. CDX-West (app_id cold-dispatch-west) calls TB-Live (app_id telemetry-prod) to verify the live West recovery trace. Requests outside the nominated recovery window were left for a separate review. A monitor challenged an earlier proposed fallback to the staging bridge. The approved run rejected that fallback before execution, and no live West request went to TB-Shadow.

This narrower recovery evidence independently supports the West-to-production temperature relation already observed in the preceding West verification report. In the same recovery dispatch, the operation generated a distinct alert. CDX-West (app_id cold-dispatch-west) calls Notify-Depot (app_id depot-notify) to deliver the West recovery alert. Acknowledgment by a depot lead is separate from the service request. The notification service did not initiate a reverse dispatch call.

## Production integrity control

Workers used the incident register only after two distinct evidence checks succeeded. TB-Live (app_id telemetry-prod) calls Ledger-Dispatch (app_id incident-ledger) to record the verified replay checksum. The register response references the observed originating transaction. It is not a new request from the register to Thermal Bridge. Tested failure examples kept their records in manual review and did not establish a shadow-to-ledger call. Integrity failures and attempts to approve a later release are distinct decisions with separate sign-offs.

## East replay package retention

The East operator assembled an approved diagnostic package containing no original shipment edits. CDX-East (app_id cold-dispatch-east) calls Vault-Manifest (app_id replay-vault) to retain the signed East replay package. The diagnostic handoff is a single observed call. The archive confirmation did not trigger any dispatch action. An earlier drawing showed a short-term route from TB-Live to Vault-Manifest to avoid another dispatch job. That route was dropped after review and never ran.

## Withdrawable source and independent evidence

A hot note that says "CDX sent it to the vault" has no validated region or environment. The reviewers kept it as an ambiguity for later investigation and did not attribute it to CDX-West. The older East cutover extract is subject to a custody check. If it is withdrawn, the independent East witness trace in the West verification report still supports the East-to-production temperature relation. The custody check does not preserve the older extract's uniquely supported ledger entry or notification call. The graph can then be recalculated from the surviving sources.
