# Recovered cockpit sources

These files are the legacy Subfleet desktop cockpit, imported verbatim from
`chief-of-staff` branch `feat/subfleet-traycer-port` at `8607b3c8` plus its
uncommitted working tree (the uncommitted part is most of it: +2,365 lines
across the four Swift files). The private recovery package
`~/subfleet-desktop-recovery/20260924T121841Z` holds checksums and restore
instructions.

The `.legacy` suffix keeps them out of `app/build.sh` and the frontend tests.
Each one is ported to the v2 daemon (`docs/desktop/design.md` §12) as files
under `app/`; the commit that does so names the `.legacy` file it came from
and deletes it here.

They talk to the v1 CLI (`~/.local/bin/subfleet-local`) and the v1 broker
socket; nothing here may be compiled into the v2 app as is (C-29.1).
