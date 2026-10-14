# Entry ALPHA-2: AtomicAppend

AtomicAppend performs a single atomic replace of the destination file for each write, so a reader never observes a partial record.

It does not provide crash-safe recovery: if the process is killed mid-write, the previous complete version can be lost along with the update that was in flight.

Multiple writers are supported through a simple advisory lock, but each has to be granted the lock in turn.
