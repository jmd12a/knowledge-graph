# West cutover and regional validation

## West production settlement

The cutover is now complete in West. The signed traffic record includes the West production gateway with registration app_id pay-west and the production clearing destination with registration app_id clear-prod. PGW-West (app_id pay-west) calls Clear-Prod (app_id clear-prod) to settle West captures. For this record the gateway is the caller. The clearing service never initiates a call back to that gateway.

## Independent East health check

The separate East health check came from the settlement collector, rather than the cutover trace. PGW-East (app_id pay-east) calls Clear-Prod (app_id clear-prod) to check East settlement availability. A successful check supports the same caller and callee registered in the earlier cutover. Neither the West and East gateway pair nor the East and West gateway pair calls one another in this check.

## Sandbox verification

The reconciliation test collector observed a completed synthetic settlement. PGW-QA (app_id pay-qa) calls Clear-Sandbox (app_id clear-sandbox) with synthetic transactions. The test gateway does not call Clear-Prod (app_id clear-prod). The similarly named production gateway app_id pay-west is listed as a comparison control and does not participate in the sandbox call.
