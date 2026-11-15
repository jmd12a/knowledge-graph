# Settlement cutover handover

## Active East production route

PGW-East (app_id pay-east) calls the production Clearing Service (app_id clearing-prod) to settle captured transactions. Clearing Service replies on the existing connection with a settlement receipt.

## West production cutover

PGW-West (app_id pay-west) receives settlement alarms on the shared dashboard. PGW-West does not call the production Clearing Service (app_id clearing-prod). West remains on its pre-cutover route.

## Unscoped sandbox probe

A connectivity ticket records "PGW called Sandbox Clearing Service (app_id clearing-test)". The ticket does not identify the gateway's region or app_id. Both deployments use the short label PGW in their regional channels.
