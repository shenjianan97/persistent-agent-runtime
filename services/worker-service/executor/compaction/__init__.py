"""executor.compaction — context-window management subsystem.

This package will house the compaction and summary-marker logic introduced in
Phase 2 Track 7.  Right now it contains only the shared state schema
(:mod:`executor.compaction.state`) so that Track 5's state schema lives in
its forward-looking home before Track 7 fields are added.  Subsequent tasks
in Track 7 will populate this package with the compaction trigger, the
summarisation node, and the related helpers.
"""
