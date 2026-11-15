# North depot cutover
The commissioning team accepted the north shift after the paper and electronic boards agreed. The following statements describe requests executed by running software.

Dispatch-N (app_id fleet-dispatch-north) calls Health-Live (app_id asset-health-live) to retrieve the current inspection reading for an active maintenance job.

Dispatch-N (app_id fleet-dispatch-north) calls Journal (app_id work-order-journal) to record the approved isolation decision.

The Health-Live product manager owns the dispatch acceptance checklist and signs the change window. Business ownership does not make Health-Live a caller of Dispatch-N. A staged Dispatch-Test request to Health-Live was rejected at the gateway and did not reach the target. A future plan to send a south alert through Pager has not been executed.

## Meter readings

The vehicle operator records the meter at arrival and the technician checks it before a service interval is closed. If the figures disagree, both values remain on the exception sheet pending inspection. Any discrepancy is assigned a named owner and remains on the local exception list until it is resolved. The supervisor notes the time and next check.

## Contractor access

Contractors receive a time-limited gate pass and report to the depot lead. Their tools are checked on entry and exit, and the pass is surrendered when the agreed work finishes. The depot retains the signed sheet so a reviewer can follow the decision and its timing. Copies are checked for legibility before originals are filed.

## Shift briefing

The outgoing lead identifies incomplete jobs, vehicles awaiting parts and any isolation that must remain in place. The incoming lead repeats the critical holds before signing the handover. The shift lead records the constraint and agrees the next action with the responsible person. A change of shift does not close an outstanding action.

## Weather contingency

Heavy rain may close the outdoor inspection lane while covered bays remain available. The coordinator moves only eligible inspections and records appointments that cannot be safely moved. This instruction describes a physical depot process. Its evidence is the signed local record and the people who completed the check.

## Equipment movement

A shunter checks the route and receives a signal from the bay marshal before moving equipment. The marshal stops movement if the path is obstructed or the destination is not ready. The duty lead checks the entry at handover and asks the responsible person to correct an incomplete record. The shift pack is kept available for review.

## Inspection evidence

Photographs are labelled with the equipment number and inspection date. A reviewer rejects unreadable labels and asks for another photograph instead of guessing which machine is shown. Any discrepancy is assigned a named owner and remains on the local exception list until it is resolved. The supervisor notes the time and next check.

## Permit control

Hot work requires a permit with a named fire watch and a defined finish time. The permit issuer checks the surrounding area again before the equipment is released for ordinary use. The depot retains the signed sheet so a reviewer can follow the decision and its timing. Copies are checked for legibility before originals are filed.

## Supplier collection

The return desk seals failed components in the appropriate packaging and records the collection reference. The supplier signs for the count; missing items are investigated at the depot. The shift lead records the constraint and agrees the next action with the responsible person. A change of shift does not close an outstanding action.

## Training cover

A trainee may observe a live job but cannot close the safety check alone. The supervising technician records the review and remains responsible for the result. This instruction describes a physical depot process. Its evidence is the signed local record and the people who completed the check.

## Appointment changes

The coordinator offers a revised slot when a repair overruns, taking account of route commitments and required rest. A missed appointment is reconciled with the operator before another slot is booked. The duty lead checks the entry at handover and asks the responsible person to correct an incomplete record. The shift pack is kept available for review.

## Tool calibration

The team removes an expired measuring tool from the shared cabinet and sends it for calibration. Work affected by an out-of-date tool is identified for a separate engineering review. Any discrepancy is assigned a named owner and remains on the local exception list until it is resolved. The supervisor notes the time and next check.

## Inventory count

The storekeeper compares the shelf count with the issue sheet at the end of the shift. An unexplained difference is held as an exception until previous movements have been checked. The depot retains the signed sheet so a reviewer can follow the decision and its timing. Copies are checked for legibility before originals are filed.

## Release check

Before a vehicle leaves, the lead verifies that guards are refitted and loose tools are accounted for. The driver receives any operating restriction in writing and acknowledges it. The shift lead records the constraint and agrees the next action with the responsible person. A change of shift does not close an outstanding action.

## Audit sampling

The auditor selects jobs across different shifts and checks signatures against the actual rota. A clean sample does not override an unresolved safety hold on an individual vehicle. This instruction describes a physical depot process. Its evidence is the signed local record and the people who completed the check.

## Incident escalation

An injury or uncontrolled movement is reported to the site lead immediately. The work area remains protected while the lead arranges assistance and preserves relevant physical records. The duty lead checks the entry at handover and asks the responsible person to correct an incomplete record. The shift pack is kept available for review.

## Consumables

The shift keeps a small buffer of gloves and absorbent material near the bay. The storekeeper replenishes the buffer after checking disposal and stock records. Any discrepancy is assigned a named owner and remains on the local exception list until it is resolved. The supervisor notes the time and next check.

## Battery handling

A battery awaiting collection is placed in the designated ventilated area with its condition marked. Damaged units are separated and the depot follows specialist carrier instructions. The depot retains the signed sheet so a reviewer can follow the decision and its timing. Copies are checked for legibility before originals are filed.

## Roster reconciliation

The scheduling lead checks absence cover before allocating specialist jobs. A person shown on the roster is not assumed to be available until the shift lead confirms attendance. The shift lead records the constraint and agrees the next action with the responsible person. A change of shift does not close an outstanding action.

## Work packs

The clerk places the checklist, permit and parts sheet in the same job pack. A missing page is requested from its owner and the pack is kept open until the issue is resolved. This instruction describes a physical depot process. Its evidence is the signed local record and the people who completed the check.

## Visitor briefing

Visitors receive the marked walking route and are accompanied inside the workshop. The guide stops the visit if a vehicle movement makes that route unavailable. The duty lead checks the entry at handover and asks the responsible person to correct an incomplete record. The shift pack is kept available for review.

## Queue review

The controller reviews jobs that have waited beyond their expected start time. The reason may be a missing part, unavailable skill or a vehicle delay, and each needs a different owner. Any discrepancy is assigned a named owner and remains on the local exception list until it is resolved. The supervisor notes the time and next check.

## Waste segregation

The technician separates oily material from ordinary waste and marks the container. The collection record records the container count, not the maintenance outcome. The depot retains the signed sheet so a reviewer can follow the decision and its timing. Copies are checked for legibility before originals are filed.

## Appendix: exception register, selected entries

| Ticket | Observation | Disposition |
| --- | --- | --- |
| N-441 | Replacement seal arrived without its batch card. | Storekeeper requested a new card; vehicle remains in the bay. |
| N-442 | The technician checked the isolation lock before removing the guard. | The lead signed the physical inspection sheet. |
| N-443 | Dispatch-N (app_id fleet-dispatch-north) calls Pager (app_id depot-pager) to deliver the north depot isolation alert. | Duty desk acknowledged the alert and kept the job open. |
| N-444 | The night supervisor identified an unlabelled parts container. | Collection was deferred pending identification. |
