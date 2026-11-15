# Fictional retail operations benchmark

This fictional scenario is inspired by the OpenTelemetry Astronomy Shop demo. The official service index lists Checkout, Cart, Payment, Shipping and Email: https://opentelemetry.io/docs/demo/services/ . The architecture is described at https://opentelemetry.io/docs/demo/architecture/ . These sources provide the retail setting only. Every app_id, alias, environment, incident, procedure and relationship below is invented for this benchmark, and is not a claim about the public demo or a real deployment.

# Service ownership registry

The app_id is the stable identity. Similar names do not establish a shared identity.

- Checkout Live (app_id store-checkout-live) is a distinct registered service for this fictional exercise.
- Checkout Test (app_id store-checkout-test) is a distinct registered service for this fictional exercise.
- Cart Live (app_id store-cart-live) is a distinct registered service for this fictional exercise.
- Payment Live (app_id store-payment-live) is a distinct registered service for this fictional exercise.
- Payment Test (app_id store-payment-test) is a distinct registered service for this fictional exercise.
- Shipping Live (app_id store-shipping-live) is a distinct registered service for this fictional exercise.
- Email Live (app_id store-email-live) is a distinct registered service for this fictional exercise.
- Order Ledger (app_id store-order-ledger) is a distinct registered service for this fictional exercise.
- Order Replica (app_id store-order-replica) is a distinct registered service for this fictional exercise.

Live and test environments are separate. A shared product label does not imply a call.

## Registry ownership

The trading operations lead owns the live checkout entry and approves any change to its accountable team. The training lead owns the test entry. Each owner records a named deputy and a contact route in the weekly staffing sheet. An expired ownership entry is an administrative exception; it does not by itself establish that traffic moved.

## Identity registration

A new service receives an app_id before the first acceptance sample is reviewed. The registration ticket contains the environment, accountable owner and intended business purpose. A familiar display name may be reused, but the ticket must not recycle an existing app_id for a different environment. The registrar rejects incomplete requests and returns them to the requester.

## Name change procedure

A team may change a display label after a reorganization without creating a new service identity. The owner records the old label, replacement label, effective date and authorizing ticket. Reports spanning the change retain the original label in their source records. A reviewer can resolve both labels to the registered identity only when the mapping is explicitly documented.

## Environment boundary

Live trading records reflect customer commitments; test records represent controlled rehearsals. The two environments have separate ownership and separate accounting controls even where their screens use the same product name. The test owner obtains synthetic basket data from an approved fixture and checks that the rehearsal is excluded from customer reporting.

## Basket stewardship

The cart record describes the items a shopper has selected before the purchase is accepted. For an investigation, the retail analyst compares the item identifiers, quantities and prices with the customer-facing receipt. A changed promotion or stale quantity can explain a mismatch, but the analyst records the applicable offer period before making that determination.

## Payment stewardship

The live payment entry is maintained under the trading control calendar; the test payment entry is maintained under the rehearsal calendar. They must remain distinguishable in reports even if both appear under a generic Payment heading. The finance reviewer uses the environment and stable identifier from the original evidence, never the label alone.

## Delivery stewardship

The delivery owner keeps the list of carrier service levels and the dates on which each level is offered. The order reviewer checks the destination region and selected service against that list. A quote can expire before a parcel is handed over, so its presence does not establish shipment or delivery.

## Confirmation stewardship

The confirmation owner maintains the approved customer wording and the escalation path for undelivered messages. A message receipt and a customer reading a message are different events. Support retains the order reference and timestamp but avoids placing a customer address in the shared incident title.

## Order records

The order ledger is the exercise record used for durable order decisions. The recovery copy is separately registered so that a restore exercise can identify its own owner and review period. Their similar business purpose does not merge their identities, and a recovery note must identify the particular copy examined.

## Registration approval

The requester attaches a control owner and an evidence retention period to a proposed entry. A second reviewer checks whether an existing identifier already describes the same service. If the request is a rename, the reviewer asks for a name-change record instead of silently issuing another identifier.

## Inventory reconciliation

At the end of each trading week the registrar compares active ownership entries with the approved change list. Differences are assigned to an owner and given a resolution date. The comparison can reveal a stale record, but it cannot prove which direction a runtime request traveled.

## Access review

Only the named maintainers may amend an ownership entry. The access reviewer checks the membership list after a staff departure and records the removal date. Investigation readers receive the minimum access needed for their case; their access does not grant permission to alter the register.

## Evidence retention

The team keeps the approved register snapshot with the corresponding review ticket. A later correction is linked to that snapshot instead of overwriting the historical decision. Retention rules apply to exports as well as the central record, and the reviewer records when temporary copies have been removed.

## Duplicate labels

Support tickets sometimes use Checkout without an environment qualifier. The registry lists both live and test entries, so that phrase by itself cannot select one. An investigator obtains the environment from the supporting ticket before assigning the observation. If the supporting material is missing, the case remains unresolved.

## Ownership disputes

If two teams claim the same entry, the duty manager freezes the proposed administrative change until both have supplied their change records. The service remains listed under its last approved owner during the review. A dispute about responsibility must not be converted into an invented runtime dependency.

## Onboarding checklist

Before a new owner accepts responsibility, the predecessor supplies the control calendar, open exceptions and current escalation list. The new owner checks that each open item has a next action and a due date. Acceptance is recorded in the handover ticket rather than inferred from attendance at a meeting.

## Retirement checklist

A retirement proposal identifies the final customer impact review, the last period requiring reconciliation and the location of retained evidence. The registrar marks the entry retired only after the owner signs off. Retirement of an entry does not erase historical observations made while it was active.

## Audit sampling

The quarterly reviewer selects a sample across both trading and rehearsal entries. For each sample, the reviewer traces the owner and environment back to a dated approval. A failed sample creates a corrective action with a deadline; it is not repaired by silently changing the old approval.

## Identifier correction

If a source document contains a mistaken app_id, the document owner issues a correction naming the affected passage and its replacement. The audit copy retains the original value and correction date. Reviewers assess downstream reports that relied on the original value rather than assuming that every similarly named entry was wrong.

## Incident routing

The incident coordinator routes an issue according to the owner of the affected business outcome. An unresolved checkout screen, disputed payment and missing delivery each require a specific case description. Co-appearance of those descriptions on an incident board is not an observation of an interaction between services.

## Training records

Rehearsal operators record the fixture version and the time window of a training run. The lead checks that synthetic order references cannot be confused with live references. Training results are useful for validating a procedure, but they are not evidence that a live customer order followed the same path.

## Control exceptions

The registry reviewer records missing owners, expired approvals and ambiguous labels as separate exception categories. Each exception has a responsible person and a next review date. An overdue exception is escalated to the control owner; it is not automatically treated as proof of an outage.

## Release notes

A release notice identifies the affected cohort and gives customer support a plain description of the expected change. It also identifies the rollback decision maker and the cutoff for accepting new cases. The release notice is planning evidence; an actual observation requires a separate record.

## Monthly attestation

Each owner signs the monthly inventory attestation after checking the registered purpose and environment. The registrar preserves the signed version and reports late attestations to operations leadership. A signature establishes accountability for the entry, not confirmation of every transaction in the period.

