# Month-end reconciliation and acceptance exercise

The team reviewed the return alongside unrelated adjustments. A match can fail because the buyer uses another purchase-order reference, payment was split across invoices, or a credit belongs to a different legal entity. The analyst documented exceptions instead of copying an amount to another account.

Desk-Live calls Ledger-Orders to confirm the order terms during reconciliation.

Refund-Live calls Ledger-Credits to record a reconciled credit adjustment.

Inspect-WH calls Notice-Buyer to send a receipt confirmation.

The receipt confirmation says a package arrived for review; it does not promise acceptance or payment. It is distinct from the decision notice in the previous report. The finance queue remains earlier in the business timeline, but the requester for the credit adjustment is Refund-Live.

Desk-Test calls Refund-Sandbox to exercise a fabricated refund.

The test used a synthetic buyer and nonfinancial instrument. Desk-Test and Refund-Sandbox are separate from their production namesakes. The test does not establish Desk-Live calling Refund-Sandbox or Desk-Test calling Refund-Live.

A proposed workflow would have had Notice-Buyer call Inspect-WH when a buyer replied to a receipt message. The team did not implement it; replies remain a manual task. A retired diagram showing Ledger-Orders calling Desk-Live is historical. The observed production lookup runs in the opposite direction.

The freight dispute remained open. The carrier produced a signed pickup document but one line was illegible. The account manager requested a clean copy and retained both versions. Finance did not silently interpret missing text as approval. The report was signed with the unmatched item visible for the next period.

## Cutoff and scope

The reconciliation covered credits approved before the month-end cutoff and compared them with the corresponding invoices. Its purpose was to identify unmatched items, not to re-decide every return. A reviewer could accept the goods decision while leaving a freight question open. The signed report therefore distinguishes completed checks from work carried into the next accounting period.

The team obtained the approved case summary and checked the invoice currency, legal entity and quantity. The order contained six units, four of which the buyer retained. The partial adjustment was consistent with the approved returned quantity. The analyst did not treat the presence of a return case as permission to reverse the entire invoice.

The buyer had paid in two installments. That fact affected statement allocation and required the analyst to inspect both payment references. It did not alter the physical inspection result or authorize a different amount. A balanced total alone would not prove a correct allocation if the credit were placed against the wrong invoice or buyer.

The buyer's receiving address belonged to a logistics contractor. The analyst confirmed the purchasing legal entity against the invoice instead of using the delivery address as a proxy. Similar names appeared in the account directory, so the entity number was included in the review record. Bank details were kept out of the general reconciliation notes.

## Open freight item

The outbound freight question remained unresolved when the original case review ended. The carrier later supplied a signed pickup document, but one line was not readable. The account manager requested a clearer copy and retained both versions. The date of the later document matters because it was not available to the earlier reviewer at the earlier cutoff.

Finance did not infer approval from the fact that the goods adjustment was accepted. The governing contract treated the freight question separately. A named owner was responsible for following up, and the unresolved amount stayed in the exception list with a next review date. Closing the goods item did not silently close the freight item.

If later evidence supports an additional adjustment, the commercial owner must document its basis and obtain the appropriate authority. The analyst can then reconcile the separately approved amount. That future possibility is a conditional procedure, not a claim that a further credit has already been issued.

A disputed charge can cross accounting periods. In that situation, the prior report remains a record of what was known at its cutoff. A later report can describe the new evidence and decision without rewriting the earlier state. This distinction lets an auditor trace the progression of the case without interpreting the order of documents as the direction of a software request.

## Receipt confirmation

The receipt confirmation was generated for physical arrival, before a commercial decision was necessarily available. It gave the buyer a receiving reference to use when asking about the package. It did not state that the contents were accepted, that the contract allowed a return, or that a refund had been paid. The wording was deliberately narrow because an unreadable label or count discrepancy may require later investigation.

A separate decision message can be sent after a reviewer approves or rejects a claim. Although both communications may reach the same buyer, they have different purposes and can have different initiating services. An inbox timeline shows when messages arrived; it does not establish the direction of a request between software components.

The receiving team checked that the package count agreed with the manifest and held the obscured package until a supervisor completed the match. The confirmation did not remove the need for inspection. If staff subsequently discover a mismatch, they record an exception and notify the case owner through the established operating process. This report makes no additional assertion about an automated interface for that exception.

## Test exercise

The acceptance exercise used a fabricated buyer, an artificial invoice and a nonfinancial instrument. Its purpose was to let trainees observe a deterministic refund scenario without affecting a real account. The UAT desk and sandbox refund engine have distinct identifiers from the production deployments, even though both pairs share display names.

The instructor also described a rejected claim outside the contractual return window. Trainees had to identify the authority needed for an exception and the evidence to retain. A discussion of how a rejection might be communicated is not proof that a notification integration ran. The training notes record the observed test request separately from classroom discussion.

The sandbox cannot settle with a real buyer. A successful test response demonstrates the behavior of the exercise within that environment; it cannot be used as evidence of a production credit. Operators should use the explicit environment aliases when transferring test findings into a support ticket, particularly because the bare word Refund can also mean the commercial action.

A proposed reply workflow was discussed during training but was not implemented. Buyer replies remained assigned to a person. A trainee's sketch of the proposed sequence is not an observation of runtime traffic. Keeping the proposal in the report is useful because it explains why a reader may encounter a tempting but unsupported reverse arrow.

## Exception handling

An unrelated account had two approvals carrying a duplicate credit reference. The analyst suspended allocation while the owner compared the approval dates and underlying invoices. Reusing a reference can make an account appear balanced while assigning value to the wrong transaction. The investigation recorded its conclusion and the evidence examined rather than silently selecting one entry.

A response interrupted during a financial operation is an uncertain result, not automatic proof of either failure or success. The operator checks the existing record using the request identifier before deciding on a retry. If a duplicate is found, the correction needs appropriate authority and an audit trail. If no record exists, the approved retry procedure applies. These instructions do not claim that such an interruption occurred in the observed case.

A receiving discrepancy is handled independently from a financial reconciliation discrepancy. A missing serial number may prevent the warehouse from matching a unit, while a missing invoice reference may prevent finance from allocating a credit. The same case can have both issues, but resolving one does not automatically resolve the other. Owners record each issue and its next action separately.

## Provenance and ownership

The original case review and this month-end report independently describe the production order lookup and the approved credit request. Their support can be tracked by source. If one report is withdrawn because it is invalid, the other still supports the observations it independently records. A relationship found only in the withdrawn report loses that source of support.

Withdrawing a document from an evidence set is a provenance operation. It is not a statement that a running service has been deleted or that an old business event never happened. Reviewers should distinguish the underlying operational state from the claims supported by the remaining documents. A retained source must actually contain the relevant positive observation; a similar label or an implied chronology is insufficient.

Commercial operations owns the return decision, warehouse staff own physical findings, and finance owns reconciliation. Their queues organize human responsibilities. An approval may precede a request without being its network caller. For the credit operation documented here, the requester remains the refund service even though the queue was populated earlier.

At the cutoff the reconciliation was signed with the freight item unresolved and assigned. The training owner planned to refresh examples for the next exercise, including missing documents and mixed currencies. Those curriculum changes are future work and do not assert further service integrations. Any future production change would require its own observed evidence.
