# Northstar chilled fulfillment service registry

## Collection scope and identity authority

This extract was signed by the ownership custodians for Northstar Logistics for the June operating cycle. A row represents a deployable application with a durable inventory identity. Fleet names, team names, role names, data products, deployment labels and ticket numbers are descriptive metadata. The registry is an inventory of custody and identities. Runtime relationships are certified in observed operating records. The team uses the display label on wallboards and the operational alias in incident chat. Use the app_id when consolidating identities from separate source records.

## Freight dispatch fleet

The East production instance of Freight Dispatch is registered as app_id cold-dispatch-east and uses the operational alias CDX-East. Its operations team owns route state for the Delta and Kestrel depots. An application with the same display name, Freight Dispatch, handles West production and is registered as app_id cold-dispatch-west under the operational alias CDX-West. Both regional teams abbreviate the current application to CDX in internal chat. That short form identifies a unique application only inside a verified regional operating record.

Freight Dispatch in the rehearsal tenant is registered as app_id cold-dispatch-uat and uses the operational alias CDX-UAT. UAT manifests use synthetic load IDs and inventory fixtures that resemble production packaging. The rehearsal tenant has its own certificate and does not represent either production route. East, West and UAT have separate service identities even when screens show identical branding, version numbers or configuration field names.

## Temperature evidence fleet

Production Thermal Bridge is registered as app_id telemetry-prod and uses the operational alias TB-Live. It owns validated carrier temperature evidence and records the outcomes of checksum and freshness validation. The staging deployment also displays Thermal Bridge and is registered as app_id telemetry-shadow under operational alias TB-Shadow. Local support chats sometimes shorten both names to Thermal. Shadow records are test artifacts. The production custodian reviews checksum failures without treating staged evidence as accepted shipment evidence.

A sensor transmits through a carrier's own equipment and sends timestamps in its own time zone. The equipment is outside the application inventory described here. The shipment identifier, dispatch number and logger serial can appear together in an investigation, and none is an application identifier. A load's inspection states are distinct from the service registration state. Re-tagging a load does not create a new deployable application.

## Incident and depot functions

The Incident Ledger is registered as app_id incident-ledger and uses the operational alias Ledger-Dispatch. It is the operations evidence register. Depot Notification is registered as app_id depot-notify and uses the operational alias Notify-Depot. It is the contact delivery function for regional leads. A shared responder, a matching correlation number, a common certificate owner or a dashboard tile can appear in both teams' notes. Those references record ownership, evidence or coordination.

## Review and approval history

The custody review checks current certificate control independently of the business continuity tabletop. Sandbox permissions expire at the end of a rehearsal. A permission row grants a possibility within the change window, and actual execution requires a separately observed transaction or an accepted recovery record. Control owners sign off the positive and exception lists separately. In particular, a label such as CDX or Thermal without its region or explicit registration remains pending identification.
