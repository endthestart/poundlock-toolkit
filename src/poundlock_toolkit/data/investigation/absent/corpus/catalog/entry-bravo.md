# Entry BRAVO-5: BravoLog

BravoLog appends new records to the end of a single log file and validates a checksum trailer on every record when the log is reopened.

A crash during a write never corrupts previously committed records, because the checksum trailer catches any record that was left incomplete.

BravoLog is strictly single-writer: a second process that opens the same log for appending is refused with a lock error, so concurrent writers are not supported.
