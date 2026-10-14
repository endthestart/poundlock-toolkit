# Module EPSILON-9

Epsilon-9 is a coordination layer that sits in front of a plain log file.

Each writer requests a sequence ticket before it is allowed to write, so any number of writer handles can be active at once without corrupting one another's records.

If the process holding the layer is terminated abruptly, the next process to open the log replays the ticket sequence and rolls back any ticket that never finished writing.

After that replay, the log reflects only fully written records, with no partial record visible to a later reader regardless of how many writer handles were active when the process stopped.
