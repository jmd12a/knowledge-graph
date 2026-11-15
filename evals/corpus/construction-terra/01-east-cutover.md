# East payment cutover verification

## Production settlement trace

The East production collector recorded a successful capture-to-settlement trace after the change. PGW-East (app_id pay-east) calls Clear-Prod (app_id clear-prod) to settle captured transactions. Authenticated traces identify the gateway as caller and the clearing service as callee. There is no reverse call from Clear-Prod to PGW-East.

## Audit delivery

The audit exporter within PGW-East, registered as app_id pay-east, calls the Audit Ledger at app_id audit-ledger to deliver the settlement event. That audit call terminates when the ledger acknowledges receipt. A row showing the Audit Ledger as responder is the response span for the same call.

## Out-of-scope incident material

The handover mentioned West only to confirm that its existing operations were unaffected by the East cutover. This trace does not show a West call.

A pasted incident line says "PGW called Clear-Sandbox (app_id clear-sandbox)" but has no region, environment, source registration ID, or authenticated trace. The label PGW is shared by East and West. The source of that line remains unresolved and the note is retained for investigation.

A rejected proposal would have pointed PGW-East (app_id pay-east) to Clear-Sandbox (app_id clear-sandbox). The proposal was never deployed.
