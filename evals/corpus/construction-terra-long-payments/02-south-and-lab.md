# South regional rollout and isolated laboratory attestation

Operations reviewed two independent event sources for 14 May. The first source covers South production captures from 16:20 to 19:00. The second covers a synthetic acceptance run in an isolated laboratory, with its own credentials, identities and accounting entries. The sign-off list contains staff from both regions because they share a change board. The sign-off does not broaden the reporting scope to North production or prove network traffic between routers.

## South production event trail

The South merchant cutover used the certificate series S014 and postponed unmatched merchant records until the later exception window. SouthPay (app_id pay-router-south) calls RiskGate-Current (app_id merchant-risk-v2) to screen South capture records. The response label carries the application that sent the reply; this is still a South-initiated request. RiskGate-Legacy (app_id merchant-risk-v1) appears in the precheck comparison, but the South router did not use it.

The initial verification compared live merchant totals to hypothetical queue depth estimates. An archived runbook showed ClearBank-Lab as the intended emergency destination, but it was superseded before the change and no such production destination was configured. SouthPay (app_id pay-router-south) calls ClearBank-Live (app_id clearing-bank-prod) to settle approved South captures. The South production certificate contains no call to ClearBank-Lab and no invocation of NorthPay.

## Laboratory result and scope check

The laboratory replay used generated fraud responses and dummy transaction IDs that cannot settle deposits. A signed observer sheet still recorded an actual service request within that isolated test environment. ClearBank-Lab (app_id clearing-bank-lab) calls RiskGate-Legacy (app_id merchant-risk-v1) to validate the laboratory replay fixture. That is an executed laboratory call. The spreadsheet names the production bank and current risk gate only as baseline comparison columns; it neither asserts an invocation between them nor authorizes carrying the test traffic into production. A draft note to rerun the exercise on the current risk version was cancelled.

An unsigned line says "Pay Router called ClearBank-Lab", but its header was cut off and the trace ID does not resolve to either regional account. The desk retained the line as an investigation item and excluded it from executed call assertions. Both regions use that shortened name. The line cannot be interpreted as a South or North runtime invocation.

## Control packet handoff

After the production cutover, audit operations confirmed that the approved packet was ready for review and that its notification channel had the correct recipient list. Vault-Write (app_id merchant-audit-vault) calls NoticeDesk (app_id merchant-notice-hub) to publish the attested South control-packet receipt. NoticeDesk accepted it for notification delivery. The distribution-list preview on the old checklist represented a proposal and no request. An invitation from a South operator to an audit reviewer is an administrative step and does not establish a SouthPay invocation of either service.

The attestation deliberately keeps the confirmed production and laboratory calls within their own scopes. It includes no new service identities, and it is not a restatement of the earlier North certificate.
