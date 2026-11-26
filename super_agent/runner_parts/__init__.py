"""Responsibilities of the launcher host, split by role (ISS-131).

``components`` holds the registry entries and their health rows: pure data
plus the two run shapes (thread body, service body). ``runner`` keeps the
scheduling - start, single-flight trigger, budgeted stop, aggregate health -
because that is where the loop and the stop semantics live.
"""
