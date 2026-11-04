"""`CommandResult.error_code` values of the three Ingest commands.

`IngestSign`, `IngestOpenCredentials` and `IngestCheckReceipt`
(proto/visio_schema/v1/control/command.proto) are refused with these. One
definition, so no port hand-types them.
"""

__all__ = ["BAD_RECEIPT", "BAD_REPLY", "NOT_INGEST_MODE", "RECORDING_IN_PROGRESS"]

# The device is recording; ask again once it stops.
RECORDING_IN_PROGRESS = "recording_in_progress"
# The device's stored destination is not an Ingest API.
NOT_INGEST_MODE = "not_ingest_mode"
# IngestOpenCredentials: the reply signature is wrong or the seal will not open.
BAD_REPLY = "bad_reply"
# IngestCheckReceipt: the receipt is not the service's signature.
BAD_RECEIPT = "bad_receipt"
