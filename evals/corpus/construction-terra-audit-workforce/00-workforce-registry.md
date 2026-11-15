# Workforce services and control boundaries

This registry identifies the services used in the fictional workforce reconciliation program. It is an identity register, not an execution log: entries and ownership descriptions below do not establish runtime calls. Later operational notes describe the observed flows for their stated dates and environments. Application IDs are stable keys; short names are names used by the control team.

| Service | Stable app_id | Role |
| --- | --- | --- |
| Close-Run | payroll-close | Production payroll close orchestrator |
| Correct-Run | payroll-correction | Production late correction orchestrator |
| Clock-Live | time-live | Active timekeeping snapshot service |
| Clock-Standby | time-standby | Warm standby snapshot service |
| Wage-Book | wage-ledger | Wage adjustment ledger |
| Worker-Post | worker-notice | Worker notice delivery service |
| Audit-Shelf | audit-vault | Signed reconciliation archive |

The informal label Clock has been used for both Clock-Live and Clock-Standby in ticket comments. That shared label is insufficient to identify a service; use the qualified name or stable app_id in a dated operational observation. Neither the presence of the standby nor its readiness to take over establishes that a caller used it. Correct-Run and Close-Run are separate applications even where the same payroll team owns their schedules.


## Population boundaries

The control population is the set of workers and earning periods approved for the named window. A late approval stays outside that set until an owner records its authority and identifies the period it changes. Counts are compared at each handoff so a missing group is investigated before the packet is signed.

## Time zones and cutoff

Cutoff dates are interpreted in the employing unit's configured time zone, with the local date and offset kept together in the control packet. A shift crossing midnight can belong to a different accounting period from the date on which its final punch is entered. Reviewers resolve the period before comparing totals.

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

## Audit questions

An auditor can follow a case from approval to the signed control totals using references in the restricted packet. The team answers questions using the relevant version of each record rather than rewriting an older approval. Any later correction is presented with its date and authority intact.

## Manual overrides

A manual override needs an explicit reason, an authorized approver and a record of the original value. The reviewer checks whether the override affects a worker, a period or only an internal classification. Unexplained overrides stay open until their owner supplies the missing evidence.

## Version control

Each packet states its preparation time and identifies the approved source versions used by the reviewer. A revised packet explains what changed and preserves the earlier signed version. This allows a later reviewer to distinguish a genuine correction from a change in presentation.
