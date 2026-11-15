# Fictional retail operations benchmark

This fictional scenario is inspired by the OpenTelemetry Astronomy Shop demo. The official service index lists Checkout, Cart, Payment, Shipping and Email: https://opentelemetry.io/docs/demo/services/ . The architecture is described at https://opentelemetry.io/docs/demo/architecture/ . These sources provide the retail setting only. Every app_id, alias, environment, incident, procedure and relationship below is invented for this benchmark, and is not a claim about the public demo or a real deployment.

# Delivery review and late identity binding

Checkout is used for both live and test environments in older tickets. This review refers to the delivery cohort, not rehearsal. No call to test payment was observed.

Night Counter calls Parcel Desk to request the delivery quote for the held order.

## Dispatch freeze at the label boundary

The operations desk put cohort D-114 on hold at 06:40 after the carrier announced that the morning sort would close twenty minutes early. The handover divides prepared orders into three groups: packed without a label, labeled but still under warehouse custody, and manifested with an accepted custody scan. Only the first group may be reassigned by the duty lead. A missing acceptance scan leaves a parcel in the second group while a supervisor compares the manifest export with the driver receipt. Booking confirmation and physical acceptance have different times and are recorded in separate columns. Staff preserve the original service-level promise alongside the replacement so the customer-impact count remains auditable.

## Quote expiry and regional surcharge

The held test order contained a standard carton and a second carton marked as oversized. The original delivery quote was created before the surcharge window and was due to expire before the later collection slot. The site policy permits the customer to retain the displayed charge if the higher carrier cost is within the discretionary band. When the difference is larger, a supervisor can approve business absorption or a customer can explicitly accept a revised basket. The quote returned for the held order is evidence of a pricing request. The shift has no signed carrier manifest for that order until the separate release register is confirmed.

## Split parcel promises

Mixed bundles break into two delivery groups if the fragile component needs different handling. The revised promise names the latest expected date and still displays each package reference individually. An item unavailable at the original site can move to the next route without changing the promise for packages already accepted by the carrier. Where the original order included a promotion conditional on buying the bundle, support sees the effective allocation rather than recalculating a promotion from the residual items. A partial shipment therefore has separate inventory and money obligations. The exception report carries the original order number plus one row for each package and an operator decision for every remaining item.

## Comparing staging demonstrations to the witness trace

A rehearsal executed with a painted label and a stock item removed from the commercial ledger. The ticket used the heading Checkout for both screens because the dashboards inherited a display string from the same release template. The rehearsal output carries a gray banner, a training reference, and a lagging clock. It demonstrates how a quotation would appear and never admitted a parcel into a carrier manifest. The selected witness trace uses the delivery cohort and a separate evidence retention label. During audit, a screenshot with the earlier gray banner stays with the rehearsal record even if both cases appear in the search results for the same display text.

## Operator station and tenant scope

The terminal used by the night shift originally served two merchant groups. When the pilot opened, the login policy changed from warehouse-based access to merchant-scoped access. Evidence for a quote includes the merchant code, currency, dispatch lane and package version. A second merchant can have the same public brand name while using another pickup calendar, so the public display name does not establish shared credentials or a shared operational history. The duty lead confirmed which merchant appears on the held record before approving release. No credentials, full postal addresses or customer identifiers are copied into the audit packet, which refers to restricted material by evidence number.

## Handling out-of-area addresses

The rate book has a special tier for postcodes served only on alternating weekdays. The morning schedule showed an available priority tier during rehearsals even though the live coverage list excludes that district. A change controller added a distinct flag for the regional restriction and verified that the same parcel remains held if the exclusion applies. A contact-center agent can offer pickup or a later standard route once that choice is confirmed. In a disputed case the archived postal district and selected route are enough to tell which price rules applied. Screenshots retain only the visible district and never reveal the individual address.

## Collection-point change during the hold

The customer can request a new collection point until the parcel leaves warehouse custody. The agent records the old point, proposed point, cutoff and reason. If the package has an accepted custody scan, the only supported workflow is a new carrier instruction accepted by the dispatch supervisor. A request in the customer portal stays pending while its disposition is undecided, and the internal sheet labels the outcome as accepted or declined only after the relevant custody check. The new address is intentionally omitted from the incident export. The review objective is to reconstruct what action was permitted from the business state at the time, including whether a second pickup could happen.

## Package scan gaps and clock alignment

At the dock, scanner six was eleven minutes behind the manifest clock. The operational digest records both device time and reception time. Evidence collected against the same parcel barcode can therefore appear out of sequence when viewed in a spreadsheet sorted by local time. To establish custody, the investigator uses the signed receipt, the dock door and manifest version, then records the permitted correction for the scanner drift. The adjustment applies only to that device during the logged interval. The shift keeps clock correction as a separate field because rewriting event times would make it impossible to reproduce the original discrepancy and might incorrectly invalidate an independent witness.

## Overflow route and cost approval

An overflow manifest from the adjacent depot was available only after a queue of eight fragile items cleared. The earlier price estimate excluded remote handling and stated that the driver would arrive within a broad collection slot. The dispatcher used available pallet positions and the handled carton sizes to decide whether the overflow plan was viable. Approval for paying the remote surcharge depended on the total for the held cohort, including items canceled later in the shift. The acceptance decision recorded the actual locked manifest version and the unused overflow proposal in separate sections. Future analysts should use the accepted version as the source for the implemented delivery route.

## Shop support scripts and customer impact

The support script distinguishes a late promise from a missed appointment. A delay late in the day can be communicated with the next expected checkpoint and an option to cancel the unshipped group. A promised morning appointment already missed requires a separate remedy decision. Support counts one affected purchase once even when all cartons have distinct parcel IDs. Warehouse counts cartons, finance counts transactions, and these three counters can legitimately differ. The incident lead keeps a reconciled list that joins the three views by purchase reference with collision checks, so a late customer communication is visible even if all the packages reached the correct warehouse.

## Damaged label exception

When an adhesive label cannot be scanned, the floor supervisor photographs the label area and checks the human-readable package reference against the bag transfer receipt. After confirming custody has not transferred, the supervisor marks the old label void before releasing a replacement. A handwritten identifier cannot be used to create a second shipment unless the carrier confirms the first never entered its network. The test desk compared one replacement label with its canceled predecessor and excluded both from the affected-order count until the warehouse noted the final outcome. A sorted list of printed labels would misleadingly count two physical dispatches for that single held carton.

## Audit treatment for a planned interface

A design appendix proposed moving price checks to an afternoon scheduling component after the pilot. It was circulated for capacity estimates and was never approved for the held cohort. A design ticket supplies an owner and a potential future traffic split but no witnessed interaction. Treat it as planning material. The accepted observation in this case instead comes from a timestamped commercial witness and remains tied to that witnessed package version. A change to display wording might otherwise make a reviewer read the new architecture backward into an older operation. The decision record includes the actual date of any approved change as part of its scope.

## Handover between retail and finance

A remote-area quote can change whether a basket qualifies for free shipment without affecting the inventory reservations. The fulfillment lead handles parcel release while the accounts analyst determines whether the increase is posted to freight expense or passed through with approval. For the held order the customer displayed price remains the reconciliation starting point. Where the total changes, the handover attaches the consent timestamp and both amounts. There is no implied relationship between the components merely because they appear on the same approval form. The fields identify the portions of the incident that separate teams can independently verify.

## Identity evidence retention

During cleanup after the pilot, some older nightly traffic exports were retained only as printed audit bundles. Those bundles use terse station labels, while the register controlled by the deployment owner carries stable application identifiers. At the time of this review the release owner obtained authorization to publish a mapping for two labels associated with the accepted cohort. The mapping cannot be automatically generalized to older rehearsal screenshots, whose marker and merchant scope differ. The mapping is filed as the final appendix after all of the operations evidence, so the audit analyst must carry the original quote observation forward while resolving the participants.

## Approval and remaining gaps

The change board accepted the release once evidence showed the held cohort reached a safe decision on pricing and custody. It listed the exceptions that remain open, including when the carrier will confirm the canceled label and whether the affected support replies were sent before the posted cutoff. Those pending checks do not change which application participants were witnessed during quotation. The signed register preserves the selected policy version and names the deployment owner as the keeper of alias definitions. The next independent review will use later data to determine customer effects, keeping its sources separate from the dispatch evidence included in this file.

## Appendix: ticket label resolution

Night Counter is an explicit alias for Checkout Live (app_id store-checkout-live). Parcel Desk is an explicit alias for Shipping Live (app_id store-shipping-live). These bindings identify both endpoints of the earlier observation. The unqualified label Checkout alone is ambiguous.
