# Returns service registry

Return Desk (app_id return-desk-prod) is production, uniquely aliased Desk-Live. Return Desk (app_id return-desk-uat) is UAT, uniquely aliased Desk-Test. These separate services share a display name. Desk is an ambiguous shorthand.

Refund Engine (app_id refund-engine-prod) is production, uniquely aliased Refund-Live. Refund Engine (app_id refund-engine-sandbox) is sandbox, uniquely aliased Refund-Sandbox. Refund is an ambiguous shorthand, and the sandbox cannot issue real credits.

Order Ledger (app_id order-ledger), uniquely aliased Ledger-Orders, records order and invoice terms. Credit Ledger (app_id credit-ledger), uniquely aliased Ledger-Credits, records issued credits. They are distinct services.

Warehouse Inspection (app_id warehouse-inspection), uniquely aliased Inspect-WH, records physical findings. Buyer Notice (app_id buyer-notice), uniquely aliased Notice-Buyer, prepares buyer communications.

This registry establishes identities only, not runtime calls. A queue, warehouse site, buyer account, or human reviewer is not a service. A process arrow showing business chronology does not establish API request direction.

## Registry governance and boundaries

The platform inventory is maintained by the integration governance group. Each record has an owner responsible for keeping its service description current and for identifying the environment in which the workload operates. The owner is a role, not a service identity. A person can hold more than one role during an incident, and a team can own several distinct services without collapsing them into one entity.

A stable application identifier survives ordinary operational changes. Moving a workload between clusters, changing a domain name, rotating an on-call team or adjusting a cost center is not by itself a reason to create a new identity. Conversely, a production installation and an acceptance installation remain separate even when their deployment manifests and release versions are identical. Their data, permissions and financial consequences differ.

The labels Desk and Refund appear in support tickets, training slides and conversations. Those words are useful to people who already know the environment, but they are insufficient for durable matching. The unique aliases in this register remove the ambiguity. A bare label should be left unresolved when the surrounding evidence cannot identify a deployment rather than assigned to whichever service was mentioned most recently.

Ledger is also a role word rather than a unique key. The order ledger represents the commercial transaction against which a return is assessed. The credit ledger represents an issued financial adjustment. Both may hold a buyer identifier and an invoice reference, and both may be discussed by the same finance analyst, but this overlap does not make their records interchangeable.

## Commercial lifecycle

A buyer starts a return by describing goods, quantities and the reason for the request. Commercial operations checks whether the account is eligible and whether the contract requires prior authorization. The request may cover a whole shipment, a single line or only part of a line. Its submission date and the date the goods arrive are different facts and should be preserved separately.

The reviewing agent identifies the legal entity that purchased the goods, the selling entity, invoice currency and purchase order. A shipping address can belong to a third-party warehouse and does not prove which entity paid the invoice. If the account name resembles another subsidiary, the agent should resolve the legal entity before authorizing a credit. A name similarity is a prompt for investigation, not permission to merge accounts.

The return reason records the buyer's claim. The disposition records the seller's decision after review. Damage, incorrect delivery and excess supply can each lead to different contract outcomes. A request for a replacement is not automatically a request for cash, and an accepted return is not automatically a waiver of freight or restocking charges. Any exception should identify the person who approved it and the evidence considered.

Partial cases deserve particular care. A six-unit shipment may include two items alleged to be defective while four remain with the buyer. The seller can accept one returned item, reject another and keep a freight dispute open. Treating the entire invoice as reversed would conceal these decisions and may overstate the financial adjustment. The audit trail needs a line-level quantity and an explicit basis for each amount.

## Warehouse evidence

Receipt at the dock establishes custody, not entitlement. Staff record the time, package count, carrier reference and visible condition before assigning goods to a case. A damaged or unreadable label may require temporary quarantine until the team compares the manifest, purchase-order reference and serial numbers. Such a hold is a controlled manual step, not an instruction to create a service relationship.

Inspection findings should separate observation from interpretation. A photograph can show a dent, a count can establish the number of units received and a test can show whether a part operates within tolerance. The inspector should not rewrite the buyer's original claim to match those findings. If the physical count and declared count disagree, a reviewer needs the discrepancy and any later correction, including who made it.

A disposition may change when new evidence arrives. For example, a unit initially thought suitable for restocking may fail a later safety check. The revised decision should retain the earlier decision and the reason for change. The service responsible for physical findings is distinct from the commercial authority to accept or reject a claim. A status displayed to a worker cannot by itself establish the direction of a network request.

## Financial controls

The value of an approved credit depends on quantity, price, currency, tax treatment and the applicable contract. The person reconciling it checks these facts against the invoice and approval. A credit record does not imply that a bank has already posted money to a buyer's account. A notification that a decision was made likewise is not a bank transfer.

Outbound freight can be disputed independently of the goods decision. One team may resolve the returned items while the carrier and account manager gather proof about freight responsibility. The open issue should retain an owner, next action and due date. Closing the goods portion should not silently close the freight issue or fabricate a second approval.

Month-end review compares issued adjustments with invoices and account statements. An unmatched entry should remain visible until its reference, currency and legal entity are established. An analyst must not select a nearby transaction merely to make totals agree. If a duplicate reference appears, the underlying approvals and timestamps need review before any allocation is changed.

A request identifier helps an operator investigate a retry, but it is not interchangeable with a purchase-order number, a case number or a credit number. Each has its own scope. An apparent duplicate can result from a repeated request, a second adjustment to the same order, or an incorrectly copied reference. The evidence required to distinguish these cases is more specific than the shared display name of an application.

## Operating and audit conventions

The inventory does not specify every interface in the estate. A later report may describe an observed request, a denied proposal, an obsolete diagram or a hypothetical future change. Those statements have different evidentiary weight. Actual direction requires a statement identifying the caller and target for the operation in question. A queue entry or an approval completed before an API request cannot reverse that direction.

The registry deliberately omits hostnames, credentials, personal addresses and buyer bank details. Such information is unnecessary for distinguishing the services and may change more often than the identifiers. Operators should retain sensitive supporting material in its authorized location and use a case reference in general review notes. A document's presence in an audit packet does not grant it authority to redefine a service identity.

Owners review the register after a service is renamed or an environment is added. A new alias should be explicit and unambiguous within its scope. If an old alias was used for two environments, the history should state that conflict instead of silently allocating the shorthand to one. A retired diagram can remain useful historical context if its status and effective period are clear.

A completed case can be reopened for a new commercial fact without changing the historical state at the earlier cutoff. For example, a carrier may supply a signed document after month-end. The new document affects the subsequent decision; it does not prove that the reviewer had that evidence when signing the earlier report. This separation of knowledge by date matters when assessing both responsibility and source provenance.

The acceptance environment uses fabricated buyers and nonfinancial instruments. Training can exercise rejected claims, duplicate references and missing documents without creating a production obligation. A demonstration plan is not evidence that an interface was deployed. The operating reports must say what actually happened and identify the environment in which an observation was made.

At annual review, governance checks whether ownership, names, identifiers and environment descriptions still agree with the approved inventory. They do not infer a service from every operational noun in a procedure. They also do not turn every business handoff into an interface. The practical objective is a concise record that lets later reviewers distinguish a service identity from the documents, queues and people involved in its work.
