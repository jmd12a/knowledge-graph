# Depot handover and exercise reconciliation
The regional support desk compared the north cutover record with an independent shift sample. A different duty team took the sample from a separate request trace.

Dispatch-N (app_id fleet-dispatch-north) calls Health-Live (app_id asset-health-live) to retrieve the current inspection reading for the duty desk's sampled job.

Dispatch-S (app_id fleet-dispatch-south) calls Health-Live (app_id asset-health-live) to check the inspection reading before allocating a repair bay.

Dispatch-Test (app_id fleet-dispatch-stage) calls Health-Test (app_id asset-health-stage) to check a simulated meter reading during the staging exercise.

The south operations manager owns the Pager service budget and signs its on-call rota; that is not a Pager request to Dispatch-S. A planned Dispatch-S request to Pager awaits approval and was not sent. Health-Test's failed request to Journal stopped at client validation and did not reach Journal.

## Inspection evidence

Photographs are labelled with the equipment number and inspection date. A reviewer rejects unreadable labels and asks for another photograph instead of guessing which machine is shown. The depot retains the signed sheet so a reviewer can follow the decision and its timing. Copies are checked for legibility before originals are filed.

## Permit control

Hot work requires a permit with a named fire watch and a defined finish time. The permit issuer checks the surrounding area again before the equipment is released for ordinary use. The shift lead records the constraint and agrees the next action with the responsible person. A change of shift does not close an outstanding action.

## Supplier collection

The return desk seals failed components in the appropriate packaging and records the collection reference. The supplier signs for the count; missing items are investigated at the depot. This instruction describes a physical depot process. Its evidence is the signed local record and the people who completed the check.

## Training cover

A trainee may observe a live job but cannot close the safety check alone. The supervising technician records the review and remains responsible for the result. The duty lead checks the entry at handover and asks the responsible person to correct an incomplete record. The shift pack is kept available for review.

## Appointment changes

The coordinator offers a revised slot when a repair overruns, taking account of route commitments and required rest. A missed appointment is reconciled with the operator before another slot is booked. Any discrepancy is assigned a named owner and remains on the local exception list until it is resolved. The supervisor notes the time and next check.

## Tool calibration

The team removes an expired measuring tool from the shared cabinet and sends it for calibration. Work affected by an out-of-date tool is identified for a separate engineering review. The depot retains the signed sheet so a reviewer can follow the decision and its timing. Copies are checked for legibility before originals are filed.

## Inventory count

The storekeeper compares the shelf count with the issue sheet at the end of the shift. An unexplained difference is held as an exception until previous movements have been checked. The shift lead records the constraint and agrees the next action with the responsible person. A change of shift does not close an outstanding action.

## Release check

Before a vehicle leaves, the lead verifies that guards are refitted and loose tools are accounted for. The driver receives any operating restriction in writing and acknowledges it. This instruction describes a physical depot process. Its evidence is the signed local record and the people who completed the check.

## Audit sampling

The auditor selects jobs across different shifts and checks signatures against the actual rota. A clean sample does not override an unresolved safety hold on an individual vehicle. The duty lead checks the entry at handover and asks the responsible person to correct an incomplete record. The shift pack is kept available for review.

## Incident escalation

An injury or uncontrolled movement is reported to the site lead immediately. The work area remains protected while the lead arranges assistance and preserves relevant physical records. Any discrepancy is assigned a named owner and remains on the local exception list until it is resolved. The supervisor notes the time and next check.

## Consumables

The shift keeps a small buffer of gloves and absorbent material near the bay. The storekeeper replenishes the buffer after checking disposal and stock records. The depot retains the signed sheet so a reviewer can follow the decision and its timing. Copies are checked for legibility before originals are filed.

## Battery handling

A battery awaiting collection is placed in the designated ventilated area with its condition marked. Damaged units are separated and the depot follows specialist carrier instructions. The shift lead records the constraint and agrees the next action with the responsible person. A change of shift does not close an outstanding action.

## Roster reconciliation

The scheduling lead checks absence cover before allocating specialist jobs. A person shown on the roster is not assumed to be available until the shift lead confirms attendance. This instruction describes a physical depot process. Its evidence is the signed local record and the people who completed the check.

## Work packs

The clerk places the checklist, permit and parts sheet in the same job pack. A missing page is requested from its owner and the pack is kept open until the issue is resolved. The duty lead checks the entry at handover and asks the responsible person to correct an incomplete record. The shift pack is kept available for review.

## Visitor briefing

Visitors receive the marked walking route and are accompanied inside the workshop. The guide stops the visit if a vehicle movement makes that route unavailable. Any discrepancy is assigned a named owner and remains on the local exception list until it is resolved. The supervisor notes the time and next check.

## Queue review

The controller reviews jobs that have waited beyond their expected start time. The reason may be a missing part, unavailable skill or a vehicle delay, and each needs a different owner. The depot retains the signed sheet so a reviewer can follow the decision and its timing. Copies are checked for legibility before originals are filed.

## Waste segregation

The technician separates oily material from ordinary waste and marks the container. The collection record records the container count, not the maintenance outcome. The shift lead records the constraint and agrees the next action with the responsible person. A change of shift does not close an outstanding action.

## Standby arrangements

A spare bay is kept clear for urgent work when the daily workload permits. The lead may release it for planned work after checking the likely effect on emergency cover. This instruction describes a physical depot process. Its evidence is the signed local record and the people who completed the check.

## Return to service

The operator receives a written restriction where a repair is deferred. The restriction has an owner and a review date, and the original defect is not marked as fully repaired. The duty lead checks the entry at handover and asks the responsible person to correct an incomplete record. The shift pack is kept available for review.

## Document retention

The depot archives signed physical records by shift and keeps the index with the retention schedule. Archive access is logged so a later audit can trace who removed a pack. Any discrepancy is assigned a named owner and remains on the local exception list until it is resolved. The supervisor notes the time and next check.

## Bay handover

The morning controller checks the physical bay against the work board before releasing an allocation. A late vehicle keeps its provisional place only until the next inspection interval; the controller writes the reason on the handover sheet. The depot retains the signed sheet so a reviewer can follow the decision and its timing. Copies are checked for legibility before originals are filed.

## Key custody

Keys move from the transport supervisor to the technician using a numbered tag. A missing signature causes the supervisor to hold the vehicle and reconcile the cabinet, rather than assigning the key by memory. The shift lead records the constraint and agrees the next action with the responsible person. A change of shift does not close an outstanding action.

## Reconciliation note

Dispatch-N and Dispatch-S have distinct app_id values despite sharing Fleet Dispatch as a display name. Health-Live and Health-Test also share a display name but are separate services. The sample trace independently confirms the north reading request.
