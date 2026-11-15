# Fictional retail operations benchmark

This fictional scenario is inspired by the OpenTelemetry Astronomy Shop demo. The official service index lists Checkout, Cart, Payment, Shipping and Email: https://opentelemetry.io/docs/demo/services/ . The architecture is described at https://opentelemetry.io/docs/demo/architecture/ . These sources provide the retail setting only. Every app_id, alias, environment, incident, procedure and relationship below is invented for this benchmark, and is not a claim about the public demo or a real deployment.

# Checkout observation and certificate

These observations belong to the live cohort. Test payment was present in rehearsal but not used for these live orders.

Checkout Live (app_id store-checkout-live) calls Cart Live (app_id store-cart-live) to read the basket for the accepted order.

Checkout Live (app_id store-checkout-live) calls Payment Live (app_id store-payment-live) to authorize the accepted order.

Checkout Live (app_id store-checkout-live) calls Email Live (app_id store-email-live) to send the order confirmation.

## Trading window

The pilot cohort covered orders placed during a single supervised trading window. The coordinator retained the start and end times in the change ticket and excluded rehearsals from the customer count. A request received outside the window required a separate review even when the same product promotion appeared on screen.

## Order selection

The reviewer selected accepted orders from the pilot control sheet before looking at the trace results. Selection included a mix of small baskets, multiple-item baskets and orders with a promotion. The sample was not intended to estimate the failure rate of every customer order.

## Basket verification

For each sampled order the reviewer compared item identifiers and quantities with the receipt shown to the customer. When a price differed, the analyst checked the effective time of the promotion and the recorded currency. A corrected display price required a separate approval and an explanation in the case record.

## Authorization review

Finance checked the authorization amount against the accepted order total and noted the transaction time. A successful authorization did not establish settlement, and an authorization with an unknown outcome was not counted as a settled sale. The reviewer recorded the payment reference in the restricted case workspace.

## Confirmation review

The support representative checked that a confirmation record existed for each accepted sample. The record was compared with the order reference and the permitted customer wording for the pilot. A missing confirmation was assigned a support case; the customer address was omitted from the shared status report.

## Timeout investigation

One pilot request exceeded the expected response interval. The operator marked the outcome unknown until the business record could be checked, because an interrupted response does not show whether the requested work completed. The team held the retry while it searched for an existing order reference.

## Duplicate prevention

Before any manual retry, the duty manager compared the original reference, amount and customer account with the exception queue. If an existing commitment was found, the team preserved it and reviewed the customer communication. If the outcome remained uncertain, the case stayed open for the next check.

## Promotion boundary

The promotion had a published expiration and an approved list of eligible products. A basket assembled before expiration could be submitted afterward, so the analyst checked the business rules in force at acceptance time. The analyst did not change the original timestamp to make the totals agree.

## Currency review

The pilot included orders in more than one currency. The control sheet grouped amounts by currency before computing totals; values were not added across currencies. A discrepancy was investigated against the original order and applicable price list before any finance adjustment was proposed.

## Partial basket change

A customer can remove an item after initially viewing the basket. The reviewer used the accepted order snapshot when checking the final amount and retained the earlier view only as context. A later basket change could not retroactively change the evidence of the accepted order.

## Receipt correction

When support identified an incorrect description on a receipt, the business owner approved replacement wording. The original receipt and correction were retained together under the same case number. A wording correction did not automatically alter the amount, payment status or fulfillment decision.

## Operator handover

The outgoing operator listed open references, the latest verified outcome and the next check time. The incoming operator acknowledged each item in the shift record. The handover distinguished observations from hypotheses so that an unverified explanation would not become a fact through repetition.

## Alert interpretation

A rise in the checkout exception count triggered a review of the pilot control sheet. The count alone did not identify a failed component or prove that every affected order was lost. The on-call checked the reporting interval and any late-arriving records before declaring the size of the incident.

## Rehearsal separation

The test payment environment was available during preparation but used synthetic references and a separate review calendar. The pilot evidence was collected from the live cohort. Similar names in a rehearsal checklist cannot establish that the test environment handled a customer authorization.

## Release decision

The release manager reviewed the accepted sample, unresolved exceptions and customer impact before extending the pilot. A green dashboard was considered alongside the finance control, not as a substitute for it. Deferred cases remained assigned after the release decision.

## Customer contact

If a shopper reported an unexpected charge, support requested the public order reference and approximate transaction time. Sensitive payment details were handled through the approved support channel. The shared incident record carried only the case number and a summary of the customer impact.

## Accounting cutoff

Orders near the daily cutoff were reviewed in the period in which their business records were recorded. Finance compared the subsequent period for late entries before escalating a timing difference. An unmatched item stayed in the exception ledger until a reviewer found supporting evidence.

## Source integrity

The certificate author retained the original observation text and the supporting sample identifiers. A second reviewer checked the environment and the time window before acceptance. If a later correction affected the certificate, it would identify the specific source and preserve independent evidence from other reviews.

## Manual intervention

A manual correction required the operator name, reason, approval and before-and-after values. The control owner reviewed the correction on the next business day. An intervention made to resolve one case did not authorize the same adjustment for unrelated orders.

## Delivery expectation

The checkout receipt stated the delivery option chosen at purchase, subject to the carrier schedule. Support did not treat a receipt as proof of warehouse handover. A customer reporting a delivery problem received a separate case for the physical shipment investigation.

## Evidence gaps

Where a trace was incomplete, the reviewer recorded which interval was missing and sought independent business records. Absence of a trace segment did not prove that an operation failed or succeeded. The incident owner set a time for the next review and retained the uncertainty in the case.

## Change rollback

The rollback decision was owned by the release manager and recorded against the pilot change. Before rollback, the team identified orders already accepted so that the customer and finance records could be reconciled. A rollback notice described the affected cohort and the point at which the decision took effect.

## Independent confirmation

The recovery team later collected its own sample rather than relying solely on this certificate. Separate evidence may support the same relationship for a different order. If this certificate is withdrawn, a reviewer must preserve any independently supported observation.

## Closure criteria

The pilot closed when owners had accepted outstanding actions, control totals were reconciled and the support handover was complete. An open customer case could continue under its own owner after the pilot ended. The closing note linked the final control sheet and the approval record.

## Sample independence

The reviewer treated each selected order as a separate business case, while noting when two references belonged to the same customer report. A sequence of retries for one reference was not counted as several independent customer purchases. The sample register documented the reason for excluding duplicate rows and retained a pointer to the original case.

## Shift escalation

An unresolved financial discrepancy was assigned to the finance duty lead, while an unclear customer message was assigned to support. The incident coordinator retained one shared timeline so that each team could see the decisions made by the other. Escalation did not mean that a suspected cause had been established.

## Pilot sign-off

The final sign-off recorded the cohort, observation period, reviewer and remaining follow-up work. The coordinator checked that each open item had a named owner and a due date before the certificate was filed. Subsequent evidence could amend a finding through the documented correction process, with the original record preserved for audit.

