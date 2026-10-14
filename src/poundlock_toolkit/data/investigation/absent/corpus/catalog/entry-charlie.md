# Entry CHARLIE-8: CharlieStore

CharlieStore offers an atomic multi-writer update model: any number of processes may submit an update, and CharlieStore serializes them internally.

Each update replaces the whole record rather than appending to it.

CharlieStore has no append operation at all, so it cannot satisfy any requirement that specifically calls for append semantics.
