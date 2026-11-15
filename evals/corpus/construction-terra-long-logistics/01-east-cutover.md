# East depot refrigerated dispatch cutover evidence

## Signed operational evidence, revision 4

This revision closes the East cutover review for the Delta and Kestrel depots. The change-board approval opened a test window between the local routing freeze and the morning dispatch. The evidence below records the observed runtime lanes and exception handling. An approval, a planned routing diagram or an isolated response span in an incident attachment is insufficient to confirm a route. Labels in the scope and decision records identify the responsible operations units. UTC is used for traces, and depot-local time is used in the staff log.

## Verified temperature evidence path

The signed production evidence covers sealed chilled loads with a matching logger serial and an accepted temperature checksum. The observer first verified the identity of the East dispatch workload from its registered client certificate. CDX-East (app_id cold-dispatch-east) calls TB-Live (app_id telemetry-prod) to validate the temperature trace for sealed chilled loads. The receiving service records a validation result under the same transaction identifier. A log entry that identifies TB-Live as the responder is the opposite span of this transaction. TB-Live did not initiate a reverse request to CDX-East.

The evidence applies only to loads that had reached the dispatch review queue. A load that remained in manual examination could preserve a valid logger packet without generating the validated dispatch trace. Staff checked that the selected traces were tied to the correct region. Neither the West dispatcher nor the rehearsal dispatcher handled these East route events.

## Quarantine escalation and notification

For one disputed load, evidence retention and operations notification formed distinct steps. CDX-East (app_id cold-dispatch-east) calls Ledger-Dispatch (app_id incident-ledger) to record the disputed seal and quarantine decision. The ledger entry captures the supervisor's statement, investigation timestamp and separate device identifier. A screenshot of a manually opened ledger record would be insufficient evidence for this automated call.

After the quarantine decision and the supervisor's witness signature were recorded, a separate automated notification was observed. CDX-East (app_id cold-dispatch-east) calls Notify-Depot (app_id depot-notify) to deliver the East quarantine alert. The shift supervisor could also send a manual message using the contact roster. That manual action is a people workflow, and the automated event is distinguishable by its signed observation record. The messaging responder did not establish a reverse service request.

## Rejected and ambiguous appendix records

A contingency proposal authorized CDX-East (app_id cold-dispatch-east) to use TB-Shadow (app_id telemetry-shadow) temporarily if validation failed before cutover. Control owners rejected the proposal before the release window. No East production trace used the shadow deployment. A training note with a valid checksum belongs to the synthetic tenant and is not production evidence. A second proposal suggested delegating the disputed load to CDX-West. The review found no East-to-West dispatch call.

An incident attachment reports only that "CDX contacted Thermal during the late shift". Neither short label identifies a deployment, the attached date is missing, and the region fields are blank. The incident lead marked that report unresolved. The only publishable runtime observations for this source are the signed records in the verified evidence sections above. Out-of-scope notes remain available to the human investigator.

## Acceptance and source custody

Accepted traces contain the originating workload identity, a responding workload identity, a direction, a dispatch trace identifier and an evidence collection timestamp. Cutover revotes are attached to the business ticket rather than represented as transfers between services. The source custodian retains the raw trace separately for later investigation if a signed extract must be withdrawn.
