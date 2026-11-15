# Meridian settlement service registry

The regional payment stack has three deployed instances of Payment Gateway. The East production instance, operational alias PGW-East, is registered as app_id pay-east. The West production instance, operational alias PGW-West, is registered as app_id pay-west. Both regions abbreviate Payment Gateway to PGW on their local incident channel. The reconciliation test environment uses the operational alias PGW-QA, app_id pay-qa.

The production Clearing Service, operational alias Clear-Prod, is registered as app_id clear-prod. The sandbox instance has the same display name Clearing Service and uses the operational alias Clear-Sandbox, app_id clear-sandbox. Sandbox traffic is synthetic. The two instances have independent service accounts.

The production Audit Ledger uses operational alias Ledger-Prod, app_id audit-ledger. The Operations Notification Hub uses operational alias Notify-Hub, app_id notify-hub. This registry records ownership and identity. A shared display name, control owner, or incident channel does not by itself establish a service call.
