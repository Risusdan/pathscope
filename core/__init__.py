"""Repo layout and dependency rules: core/ may depend on the stdlib and
PyYAML only (core/target/flows.py and topology.py load YAML); pyOCD is
confined to core/adapter/pyocd_swd.py, never imported elsewhere under
core/. numpy and Qt are strictly under ui/ and never appear in core/.
Individual module docstrings that say "stdlib-only" are describing that
module's own imports, not this package-wide rule."""
