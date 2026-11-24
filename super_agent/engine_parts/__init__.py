"""Pure helpers extracted from ``Engine``.

Each module here owns one responsibility and works on plain data, so it can be
read, reasoned about and tested without constructing an engine. ``Engine`` keeps
same-named forwarders, which is why moving logic here never changes a call site.
"""
