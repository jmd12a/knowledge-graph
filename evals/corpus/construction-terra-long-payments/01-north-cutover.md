# North merchant settlement cutover — execution certificate

The North pilot started at 08:10 on 12 May. This certificate covers the North production merchant queues between 08:10 and 11:40. It reconciles independent trace, settlement receipt, and security control records. Read-only review, halted work, and proposed connections are described for audit. The word approved addresses control authority and does not indicate that an approved action was executed.

## Registration cross-reference and capture review

For this certificate, the alias NorthFeed is explicitly the Payment Router instance registered as app_id pay-router-north. NorthFeed is a trace label used in the merchant treasury feed, equivalent to the operational alias NorthPay on North production captures. The alias does not describe the South router. The South operator helped sign off the checkout and did not run pilot traffic. Neither sign-off nor presence in the incident channel is evidence of an invocation.

The first merchant sample was held until screening response code M2 and the beneficiary control stamp were available. The pilot log records caller and callee independently. NorthPay (app_id pay-router-north) calls RiskGate-Current (app_id merchant-risk-v2) to screen pilot captures. A rejected legacy plan listed RiskGate-Legacy (app_id merchant-risk-v1) for screening. The reviewer signed the rejection; the legacy route never ran. The RiskGate-Current message seen in the return column is the response to the North invocation.

## Treasury receipt and settlement trace

The treasury feed exports compact event labels, then the matching payment receipt supplies the accepted attempt number. Each production attempt reconciles the trace parent ID and clearing receipt ID before it is counted. NorthFeed calls ClearBank-Live (app_id clearing-bank-prod) to submit cleared merchant captures. This attested event uses the North application identity stated in the registration cross-reference above. It is specific to the successful pilot attempt. A reversed sequence in the UI lists the clearing reply before the original caller for layout reasons; it does not show a clearing-initiated request.

The laboratory bank name appears on the compatibility matrix because it was the proposed contingency if the pilot exceeded the queue limit. No North production capture was submitted to ClearBank-Lab (app_id clearing-bank-lab). A dropped chat excerpt reads "Pay Router called ClearBank-Lab" but the team could not identify its region, capture, environment, or caller account. No runtime assertion may be attributed to that excerpt.

## Audit delivery and exclusions

The signed audit signal records the caller as NorthPay and the required evidence store as Vault-Write. NorthPay (app_id pay-router-north) calls Vault-Write (app_id merchant-audit-vault) to lodge the pilot control packet. The audit-store acknowledgement closes that request. Legal and merchant operations approved the retention choice; the controls paper has their names under approvers and no service call from Audit Vault to Operations Notice Hub.

A rejected remediation would have connected NorthPay directly to SouthPay (app_id pay-router-south) for spillover. It was never applied. This certificate does not cover refund traffic, laboratory demonstrations, or activity after 11:40. The only attested transactions for this certificate are the screening request, the clearing submission, and the audit lodging above.
