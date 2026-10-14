# Entry DELTA-3: DeltaJournal

DeltaJournal implements a write-ahead journal with crash-safe recovery: on restart, any incomplete record at the tail of the journal is discarded.

DeltaJournal explicitly does not support more than one concurrent writer; a second writer attempting to open the journal blocks indefinitely rather than being coordinated.

For requirements that need more than one concurrent writer, consult the capability index for a module that layers coordination on top of a journal like this one.
