# West chilled dispatch verification and rehearsal acceptance

## Operating record boundaries

This report follows West ramp-up and a separate rehearsal conducted under the same change ticket. The verified West production lane applies to sealed chilled dispatches from Harbor and Juniper. Rehearsal lane R uses synthetic orders and an isolated tenant. The business ticket retains both lanes and also links to a production trace independently sampled from East to compare the evidence standard. The same screen headings and approval template appear in both lanes. Their trace signatures and registration assignments are separate.

## Lane W production observations

The supervision team selected records after the local warehouse backlog was drained and before a new route was released to the carrier. The transaction coordinator verified the production logger checksum and bundled the evidence with the same dispatch correlation ID. Attachment D identifies the registered applications used by the verified West lane. CDX-West calls TB-Live to attach the verified temperature trace to the active West dispatch record. The receiver's response preserved the same dispatch ID and signed a freshness outcome. That response is correlated with the initiating event, and it does not independently certify a reverse service call.

Dispatch traces for the Harbor route were accepted only where the logger serial corresponded to the original sealed tote and the sensor timestamp could be reconstructed in UTC. A delayed result with a valid checksum remained pending until the regional witness checked the live shipment record. The witness approval affected whether the shipment could be released, and the approval itself was a manual operation. Temporary approvals in the test tracker are retained as control decisions.

## Lane R rehearsal observations

The rehearsal supervisor first checked synthetic package IDs against the isolation manifest. CDX-UAT (app_id cold-dispatch-uat) calls TB-Shadow (app_id telemetry-shadow) to validate synthetic logger packets in the rehearsal tenant. Test plots use the same unit labels and chart ranges as production. This use of the shadow deployment remains valid only in rehearsal. There was no CDX-UAT request to TB-Live, and no CDX-West request to TB-Shadow in the production sample.

## Independent East witness sample

The compliance witness selected a prior East run independently of the East cutover team's signed extract. CDX-East (app_id cold-dispatch-east) calls TB-Live (app_id telemetry-prod) to check East chilled-route evidence. The witness report stores the signed caller record and routes the result to the originating transaction. It does not certify the East quarantine ledger entry or the East notification event. The witness evidence remains useful if the previous cutover extract is withdrawn because the source trace and selection are separate.

## Excluded approval and correlation material

A dashboard tile displays both production dispatchers next to one another. Similar release timestamps do not show an East-to-West or West-to-East request. A preflight approval mentioned Notify-Depot as a possible West target. The supervisor cancelled the planned message before any West notification was generated in this verification window. The shared label CDX in the approval sheet cannot resolve a deployment outside the authenticated lane.

## Attachment D. Verified registration assignments

For lane W, CDX-West is the operational alias of the application registered as app_id cold-dispatch-west. TB-Live is the same registered application as app_id telemetry-prod in the verified West and independent East production traces. For lane R, the rehearsal registry assigns CDX-UAT to app_id cold-dispatch-uat and TB-Shadow to app_id telemetry-shadow. The attachment is the endpoint identity authority for the observed lane descriptions, including the earlier West paragraph where staff kept the short aliases used in the trace view.
