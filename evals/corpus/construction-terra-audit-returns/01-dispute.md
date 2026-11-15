# March dispute review

Six industrial valves were shipped and two returned after the buyer reported damage. The invoice contained a separate freight charge. The reviewer needed the original agreement and physical findings before approving an adjustment.

The earliest process sketch placed Credit Ledger before Refund Engine because the finance queue receives a proposed adjustment first. It proposed Ledger-Credits calling Refund-Live after approval. The interface review rejected that direction; queue chronology did not establish request direction.

Desk-Live calls Ledger-Orders to retrieve the original order terms.

Desk-Live calls Inspect-WH to obtain the recorded inspection result.

Refund-Live calls Ledger-Credits to record the approved credit.

The queue was populated earlier, yet the observed API request runs from Refund-Live to Ledger-Credits. Ledger-Credits does not call Refund-Live for this operation. The approved amount excludes disputed freight pending separate evidence.

Desk-Live calls Notice-Buyer to send the decision.

The message identifies accepted quantity and approved amount. It does not promise when a bank will post the adjustment.

Desk and Refund are ambiguous across environments. A proposed Desk-Test to Refund-Sandbox demonstration was cancelled before implementation. Desk-Live does not call Refund-Sandbox for this case.

An old diagram depicts Inspect-WH calling Desk-Live when inspection finishes. That connector was retired before this period and was not active. The observed request above runs the other way.

The dock supervisor held an obscured package separately while checking the carrier manifest. The inspector later recorded impact damage to one unit and packaging damage to the other. The commercial agent checked the agreement and recorded the basis for the decision. Finance checked currency and buyer legal entity before approving the goods adjustment.

## Receiving shift handover

The first truck arrived near the end of the receiving shift. The carrier listed two packages, while the buyer's advance notice mentioned two units without distinguishing cartons from individual pieces. The dock worker photographed the exterior and recorded the seal condition before opening either package. That sequence preserves evidence of arrival condition even if the contents require a later specialist inspection.

One label had been wet in transit. The worker could read part of the purchase-order reference but not the case number, so the package remained in the holding area. A supervisor compared the carrier manifest with the buyer's advance notice on the next shift. The package was assigned to the case only after the remaining reference and the physical contents agreed. A manual match does not imply a network request.

The two returned valves had different damage. One had a visible impact mark on its housing; the second had intact housing but torn packaging. The inspector measured the first unit against the acceptance procedure and recorded the result separately from the buyer's narrative. The buyer's original account was retained because a later reviewer might need to understand why the return was requested.

The warehouse did not decide the credit amount. Its responsibility was to establish the count and condition and to preserve any discrepancy. The commercial reviewer then compared those findings with the contract. This division matters because a damaged package can support several outcomes, including inspection, repair, restock with a deduction or rejection. A physical finding should not be treated as the financial decision.

## Commercial review

The agreement allowed a return for carrier damage if the buyer notified the seller within the stated period. The request arrived before the deadline, but the outbound freight allocation depended on a different contractual clause. The agent therefore considered the goods and freight separately. This avoided delaying a supported partial decision while preserving the unresolved question for the account manager.

The reviewer checked the invoice line against the six units originally shipped and confirmed that four had been retained. An adjustment for the full invoice would have exceeded the scope of the claim. The approved amount reflected the accepted quantity and contract price, with the disputed freight excluded. The note recorded the reason rather than relying on a later reader to reconstruct the calculation from a total.

A second person checked the buyer legal entity and the currency. The account directory contained a similarly named subsidiary, and the receiving location was operated by a logistics contractor. Neither the shipping address nor the similar account name was sufficient to establish the payer. The invoice reference and legal entity number resolved the question for this case.

The buyer had requested a replacement in the initial email, but the parties later agreed to a credit for the approved units. The final decision referenced the agreement and its date. An old draft that mentioned replacement remained in the case history with its draft status visible. Keeping that record does not mean a replacement shipment occurred.

## Finance handling

The finance queue recorded the proposed adjustment before the request documented above. That queue is a human work list: an analyst checks authority, amount and account, then marks the item ready. Its position in the sequence says when finance worked on the case. It does not establish which application initiated the subsequent request, and it cannot make the rejected architecture proposal an observation.

The analyst compared the approved quantity with the invoice and checked the tax basis. The buyer had made partial payments, so settlement allocation would require separate reconciliation. A credit entry and a cash posting are not the same event. The decision notice therefore described the approved adjustment without promising a particular posting date.

A retry must be investigated with its original request identifier. If the response was interrupted, an operator first checks whether an entry already exists before authorizing corrective action. Blindly creating a second credit could overstate the buyer's balance. If the first request failed before a record existed, the operator records the outcome and follows the approved retry procedure; an uncertain outcome is not silently classified as success.

The commercial owner retained authority to resolve the freight amount. A carrier scan was absent from the initial packet, and the buyer supplied a different copy of the delivery receipt. Finance marked that item open rather than filling in a value based on the accepted goods. The owner and next review date remained visible in the case summary.

## Buyer communication

The agent reviewed the message for consistency with the decision. An early draft implied that both units would be replaced, although the approved disposition was a credit. The final wording identified the accepted quantity and stated that freight remained under review. The edit changed the wording sent to the buyer; it did not alter the recorded inspection or create a new financial approval.

The recipient list was checked against the contact permissions on the buying account. A warehouse recipient can receive delivery information without being authorized to receive full financial detail. When contact preferences were unclear, the account manager confirmed the appropriate business contact. A failed delivery should remain visible to the case owner so that the buyer is not assumed to have been informed.

The decision notice and a future receipt acknowledgment serve different purposes. A receipt acknowledgment may only say that the goods reached the warehouse. A decision notice follows review and states the outcome. The same recipient and subject line do not prove the same sender or network direction. Each operational observation must be read in its own context.

## Evidence and exceptions

The review packet contained the buyer request, invoice line, manifest, photographs, inspection notes and approval. These documents have different authors and dates. The manifest supports the package movement; the photographs support visible condition; the approval supports the commercial decision. None should be substituted for another simply because all carry the same case reference.

An inspector corrected a serial number after comparing it with a close-up photograph. The correction retained the original value, the corrected value and the reason. This is different from deleting an inconvenient finding. An audit reader can see what changed and decide whether the correction affects the disposition. The case was not closed until the reviewer acknowledged that correction.

If the physical count had remained unresolved, the reviewer would have held the affected quantity rather than inventing an inspection result. If the buyer's legal entity had remained ambiguous, finance would have paused allocation. These are conditional handling instructions, not statements that either failure occurred in this case. They also do not introduce additional service calls.

The old printed diagram mentioned above was left in the packet to explain a common source of confusion. Its retirement date and the rejected proposal are material context. The request observations are the short statements naming both endpoints. The business sequence began with a proposed credit in a queue, but the actual financial request originates from the refund service.

## Open work and cutoff

At the review cutoff, the goods decision was approved and communicated while freight remained open. The account manager was to obtain a clearer carrier document and compare it with the buyer's copy. A later agreement could produce a further adjustment, but this report does not assert one. Any additional decision would need its own authority, amount and evidence.

The warehouse supervisor also planned to replace the damp holding-area label and add the receiving reference to the shift handover. These tasks improve traceability for staff. They are not evidence of an integration between the services named in this report. Likewise, a planned form field for packaging condition is a proposal rather than a deployed interface.

The reviewer signed the case summary with the unresolved freight item explicitly listed. This preserves what was known at the time without presenting the dispute as fully settled. Future evidence can change the eventual resolution, but it cannot retroactively turn a rejected proposal or an obsolete arrow into a recorded production call.
