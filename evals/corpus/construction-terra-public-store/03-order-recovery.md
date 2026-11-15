# Fictional retail operations benchmark

This fictional scenario is inspired by the OpenTelemetry Astronomy Shop demo. The official service index lists Checkout, Cart, Payment, Shipping and Email: https://opentelemetry.io/docs/demo/services/ . The architecture is described at https://opentelemetry.io/docs/demo/architecture/ . These sources provide the retail setting only. Every app_id, alias, environment, incident, procedure and relationship below is invented for this benchmark, and is not a claim about the public demo or a real deployment.

# Recovery and certificate withdrawal

A separate recovery sample confirmed these operations, independently of the earlier certificate.

Checkout Live (app_id store-checkout-live) calls Payment Live (app_id store-payment-live) to authorize the recovered order.

Checkout Live (app_id store-checkout-live) calls Order Ledger (app_id store-order-ledger) to record the recovered order.

Order Ledger (app_id store-order-ledger) calls Order Replica (app_id store-order-replica) to verify the recovery copy.

## First reconciliation boundary

At 08:10 the incident lead exported a frozen order list before releasing any paused transactions. The export excludes abandoned baskets and estimates returned before the buyer authorized a purchase. Financial controls count transactions by business reference and amount, with a distinct column for network confirmation. The store has no permission to infer that authorization succeeded merely because a checkout receipt was created. Operators reconcile cases with known durable outcomes first and leave the uncertain group on hold until authoritative evidence arrives. A recovered purchase can therefore leave the checkout queue while its end-of-day settlement validation is still pending.

## Restore marker selection

The restore team selected a backup sequence just before the interruption where the purchase journal was known to be internally complete. A later snapshot included partial events that could not be tied to a final order state. Restoring the later snapshot would have shortened the apparent outage but made it harder to distinguish committed and retried purchases. The recovery decision records the marker, data partition, clock source, and the approver who reproduced the checksum. Analysts use the selected marker as the boundary for replay eligibility. They preserve the later files as read-only evidence for discrepancies, and do not turn their partial events into accepted history.

## Deciding whether replay is permissible

A submitted purchase enters the replay list only if the durable store lacks an accepted row for the same merchant and business reference, or if a successful verification proves the committed row and its total agree. If a prior attempt has an unknown outcome, the status remains uncertain until a financial control resolves it. The intervention log preserves the original idempotency value and any operator-generated reference. A replay can deliberately avoid redoing the billable step when that portion is already durable. The validation sheet distinguishes a duplicate response from a genuine second authorization, with separate reasons and settlement actions for each.

## Handling an authorization after a timeout

One sample showed a client timeout before the buyer returned to the home page. The provider's authorization had been recorded within the allowed window, but internal confirmation reached the storefront after the original screen closed. The recovery owner compared the authorization amount, the basket version and the register time before accepting the existing operation. No balance-sheet adjustment was booked merely because the browser timed out. When the buyer retried, the confirmation flow displayed the earlier purchase reference. Finance keeps the provider status and customer screen response in different fields to avoid classifying the sale as two separate recoveries.

## Reconstructing the purchase record

The restored register required currency, tax jurisdiction, delivery choice, item allocations, promotion version, authorization status and merchant scope. The total paid by a buyer can legitimately differ from the sum of base item prices once discounts and regional taxes are applied. The workshop compared the persisted line totals to the exact basket revision used at authorization, keeping the original decision time rather than substituting current prices. If the journal did not carry a promotion revision, the case was left open instead of reconstructing a discount from a public offer. That preserves the distinction between an expected purchase and a proved durable record.

## Order copy acceptance

The recovery destination is permitted to hold a verification copy for a bounded interval while the primary record is stabilized. The team audited data fields in the copy at the chosen restore marker, including discount rounding and the buyer's dispatch choice. When the original has a newer event than the copy, a yellow status indicates expected lag and is recorded separately from a mismatch in copied bytes. The operations lead required two independent sample checks before using the copied record to help release the batch. Readiness of the destination says that it can accept verification work, while proof for an individual purchase still comes from its sampled receipt.

## Exchange rates at the settlement cutoff

The retailer prices the storefront in the buyer's currency and books provider settlement in another currency where appropriate. The recovery team used the stored conversion policy version and the original effective time for each affected purchase. Events delivered after the finance cutoff appear in the next batch even when the order time predates it. The investigator compared totals in the same currency and used an exchange difference classification only after comparing the actual rates. Unexplained differences stayed open. The deduction worksheet preserves the original value and provider outcome so a late refund can be reconciled without reopening unrelated cases.

## Canceling an unshipped purchase

Customers who requested cancellation during the incident were grouped by custody state, payment status and time of the request. An order with no custody scan and authorization only can generally be voided within the normal approval path. If a package is already with a carrier or a capture is booked, an operator uses the return or refund path and records how the customer was informed. The intervention review does not infer that shipment occurred from a printed label. An accepted carrier receipt remains the observable criterion for transferred custody in the decision record. Canceling a shipment also leaves the initial authorization history available for audit.

## Independent witness retention

The earlier observation certificate was prepared by a release operator who took screenshots from a dashboard that grouped two environments. A second observer recorded the recovery trace with a separate start time and an independent sample reference. The later packet stores network outcome, matching merchant code and the operation identifier without copying the original certificate. A compliance reviewer can therefore invalidate the earlier observation without discarding the recovery event. The form has explicit sections for prior incident context and new witness evidence. When both mention the same repeated service behavior, each is indexed by its own original evidence source.

## Scope of the certificate correction

The certificate correction identifies one version of one file, whose environment and claim scope were disputed during follow-up. The author attested that the correction did not apply to the later recovery trace and did not change the separate dispatch evidence. The approver signed a list of source references to supersede instead of listing business entities to remove. Existing service names may still be verified from the registry even if the corrected file supplied additional aliases. The filing procedure links historical versions for accountability and removes the superseded file from the active set used to establish current observations.

## Edge case when a customer retries twice

During the incident an operator received two calls from a customer who had submitted the same basket from two browser tabs. The visible status messages differed because the tabs rendered before and after the sequence was committed. The audit groups these by merchant and purchase key and lists every technical attempt, including the duplicate result, without giving each tab a financial row. The customer-impact metric counts one case. A second delivery promise only counts if a separate release record exists. The recovery packet preserves enough event ordering to show what the buyer saw on each tab while keeping personal session details out of the shared dataset.

## Sampling and exception coverage

The acceptance sample includes an ordinary order, an expired quote, a recovered authorization, a manually verified copy and a purchase still waiting for disposition. The reviewers did not choose only green examples from the final dashboard. They saved the inclusion criteria and compared observed data to the expected ownership, receipt state and accounting effect. Features planned but not deployed stayed in the risk register. Evidence for a ready screen alone is insufficient to close an open question. When an example fails a check, the tracker records the hypothesis and next verification time instead of replacing it with a more convenient sample.

## Preserving order identity during data moves

The migration started with a retained table of stable identifiers keyed by merchant scope and the original business reference. A report row used a display label that could have been reused a month later. The recovery team validated original identifiers before merging the reports and preserved the registration for existing entries. A moved row can have a different file offset or deployment owner without becoming another service. Conversely, an identical display label does not resolve an identity when the registrations differ. These rules let a future reviewer compare recovery snapshots over time and determine when a genuinely new observation changed the graph.

## Restoring normal triage

After accepting the initial cohort, the duty lead changed triage to monitor only attempts within the actual outage period. Routine stale baskets were returned to the standard aging workflow. Escalation remained available for a confirmed duplicate authorization, a quantity mismatch or an unverified copied record. The handover gives teams the expected time for new settlements and the last point at which a customer can change an unshipped group. The duty lead retains the original filter and records which exceptions remain open so later audits can reproduce both recovery-boundary decisions and customer notifications.

## Sign-off and residual responsibility

Deployment signs for the restored data marker, retail operations signs for the intended order state, and finance signs for reconciliation totals. A single incident owner retains the relationship between these sign-offs and the underlying samples. The authorization outcome, order record and copy check each carry independent evidence references. The final report states exactly which earlier artifact is withdrawn from active evaluation and who can resolve a remaining question. Independent findings held in another packet remain attached to that packet. This separation is important when a later audit asks why an observation persisted after the initial certificate was corrected.

## Certificate correction

The earlier certificate remains a historical record. After import, the benchmark exercise withdraws source 01-checkout-observation.md from the graph: remove only its contributions. The recovery and delivery observations remain valid. This is a fictional evaluation instruction.
