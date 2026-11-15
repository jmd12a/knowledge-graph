# Regular close control record

This dated fictional close record covers the active production run and its reconciliation handoff. It is an operational report, not an instruction to execute the run. The registry supplies the stable application IDs. Proposed paths, old diagrams and standby readiness notes are recorded as context and do not describe additional observed calls.

At the approved cutoff, the close team froze the employee and time populations, noted exceptions that required review, and recorded the evidence needed to account for the signed totals. Close-Run (app_id payroll-close) calls Clock-Live (app_id time-live) to obtain the approved time snapshot for the regular payroll close. The snapshot was versioned before reviewers compared it to the close control totals; a mismatch stopped the affected population rather than silently changing a rate or an employee's approved hours.

Once the exception owner signed the adjustment, Close-Run (app_id payroll-close) calls Wage-Book (app_id wage-ledger) to record the approved reconciliation adjustment. The ledger receipt was attached to the close packet so that a reviewer could trace the approved amount to the affected reconciliation group without using the worker's personal identifiers in this document.

The following handoff was written as one operational paragraph because the team used it as a single checklist: payroll checked the signed control totals; the timekeeping owner confirmed the snapshot timestamp; a reviewer separated the unmatched items from the settled population; Close-Run calls Worker-Post to deliver the worker reconciliation notice. The notice included the case reference and review route, while the ledger receipt stayed in the restricted packet; the coordinator then recorded who accepted the handoff and when the freeze could be lifted.

The standby was healthy but did not receive this close traffic. Close-Run did not call Clock-Standby for this run. An old sketch reverses the request arrow and labels it Clock-Live to Close-Run; that sketch was superseded before execution, and Clock-Live did not call Close-Run. A proposed Close-Run to Audit-Shelf integration remained on the next-quarter backlog and was not executed during this close. The shorthand Clock appears on a meeting agenda for both clock services, so it cannot establish an endpoint by itself.


## Duplicate submissions

A duplicate correction is identified by its business reference and effective period, not merely by identical amounts. The owner checks whether the second request replaces the first or is a separate authorized adjustment. The review record preserves the decision and its approver for later examination.

## Missing approvals

An incomplete approval is held in an exception queue with a reason and an accountable owner. The coordinator records the expected next review date and does not infer consent from a manager being copied on an email. A released batch includes only items with the required approval evidence.

## Rounding review

The control sheet retains both the source precision and the rounded payroll amount for each reconciliation group. Reviewers compare aggregate rounding differences with the approved tolerance and investigate anything beyond it. A small difference is still explained when it changes an individual result.

## Retroactive rates

A rate change effective in a prior period is checked against the authorization that established both the rate and its effective date. The calculation distinguishes the original payment from the adjustment so that a reviewer can reproduce the difference. Changing the current rate alone does not prove a historical entitlement.

## Shift transfers

When a worker moves between cost centers during a shift, the allocation record identifies the boundary and responsible owner. The total approved duration must still agree with the underlying time record. A cost-center reassignment is not automatically a correction to the worker's paid hours.

## Control totals

The packet includes a count, an amount and a period label for each reconciliation group. Reviewers compare the totals at the same level of aggregation and investigate differences before signoff. A matching grand total alone cannot prove that individual adjustments were assigned to the right group.

## Exception aging

Open exceptions are ordered by due date and by the impact of the unresolved item. The owner updates the status after review and records a reason for any extension. Escalation changes accountability for the follow-up; it does not itself approve the underlying payroll change.

## Evidence access

The signed packet is restricted to people with a documented business need. A public ticket carries a case reference rather than a worker identifier or calculation details. Access reviews consider both current role membership and whether the evidence is still required for the stated purpose.

## Receipt matching

The reviewer matches the receipt reference, effective period and total to the approved batch. If one field differs, the discrepancy remains open until the responsible owner supplies an explanation and supporting material. Copying a receipt into a packet does not by itself establish that it belongs to the batch.

## Separation of duties

The person proposing an adjustment cannot provide the independent approval for that same item. When a small team shares responsibility, the coordinator records an alternate reviewer and the reason for the assignment. The control is evaluated for each adjustment rather than assumed from the organizational chart.

## Reopened cases

Reopening a settled case requires a reason that points to new evidence or a corrected approval. The previous conclusion remains visible alongside the new decision so reviewers can understand the sequence. The owner identifies whether any worker notice or accounting adjustment must also be reconsidered.

## Batch completeness

Before release, the coordinator compares the list of eligible cases with the list included in the batch. Held cases remain on a separate list with a reason and next owner. This comparison prevents a partially approved case from disappearing merely because the overall batch was accepted.

## Dispute route

Workers receive a case reference and a clear route to raise a discrepancy with the payroll team. The support agent verifies the request and routes it to an authorized reviewer without exposing another worker's information. A dispute starts a review; it does not automatically reverse an approved adjustment.

## Retention boundary

The retention schedule applies to the evidence packet and the approvals it contains. A legal hold can suspend ordinary disposal for the relevant material, and the custodian records the reason and release authority. The case summary is kept distinct from supporting records with more restrictive access.

## Correction sequencing

Corrections are evaluated in effective-date order when one change depends on another. The reviewer checks whether a later request assumes an earlier one has already been accepted. If that dependency is unresolved, both the case owner and the payroll reviewer can see why the later item is held.

## Reconciliation ownership

Each difference has one accountable owner, an explanation, and a documented disposition. Ownership can transfer across teams, but the handoff records who accepted it and what evidence remains outstanding. The final reviewer checks that no item was closed solely because the deadline passed.

## Calendar exceptions

Holidays and payroll calendar changes can shift the operational cutoff without changing the period to which hours belong. The coordinator records the applicable calendar version in the packet. Reviewers use that version to assess timeliness rather than assuming the usual weekly schedule applied.

## Quality sampling

A reviewer samples settled items for approval, period and amount, and records both the sampling basis and any defects. A defect is traced to the affected population before the owner chooses the correction. Sampling supports the wider control but does not replace the complete batch totals.

## Handoff acknowledgement

The receiving owner acknowledges both the population and any known unresolved items. A handoff is incomplete when the sender records a delivery but no owner accepts responsibility for the follow-up. The acknowledgement is retained with the control packet and reviewed when exceptions age.

## Manual overrides

A manual override needs an explicit reason, an authorized approver and a record of the original value. The reviewer checks whether the override affects a worker, a period or only an internal classification. Unexplained overrides stay open until their owner supplies the missing evidence.

## Version control

Each packet states its preparation time and identifies the approved source versions used by the reviewer. A revised packet explains what changed and preserves the earlier signed version. This allows a later reviewer to distinguish a genuine correction from a change in presentation.

## Population boundaries

The control population is the set of workers and earning periods approved for the named window. A late approval stays outside that set until an owner records its authority and identifies the period it changes. Counts are compared at each handoff so a missing group is investigated before the packet is signed.

## Time zones and cutoff

Cutoff dates are interpreted in the employing unit's configured time zone, with the local date and offset kept together in the control packet. A shift crossing midnight can belong to a different accounting period from the date on which its final punch is entered. Reviewers resolve the period before comparing totals.
