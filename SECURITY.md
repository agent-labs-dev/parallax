# Security policy

## Reporting a vulnerability

Please do **not** open a public issue. Report it privately through GitHub's
private vulnerability reporting ("Report a vulnerability" under the
repository's Security tab) at
[the private reporting form](https://github.com/agent-labs-dev/parallax/security/advisories/new). Reports become private GitHub security
advisories: they are visible only to maintainers and the reporter until a fix
is published.

We aim to acknowledge reports within 5 working days and will agree a disclosure
timeline with you. By default we:

- confirm and reproduce the report privately;
- ship a fix and a public advisory in the same release;
- credit you in the advisory unless you ask not to be;
- withhold details until the fix is out.

Please include a short description, the affected version, a minimal
reproduction or evidence, and any proposed mitigation. Do not include secrets
or customer data.

## Scope

Parallax runs third-party agent harnesses against a mock world, so the security
boundary matters. In scope:

- escape from `DockerBox` isolation, or egress beyond the sidecar (world server
  and meter);
- reading the world server's control API, another trial's world, or trusted
  verify output from inside the box;
- a harness under test influencing its own grade other than through world state,
  workspace or answers (grader tampering);
- leaking the metering proxy's upstream credentials to the box;
- secrets or private data in the published tree, wheel or images.

Host isolation trusts the harness and does not provide a sandbox boundary.
Report credential leaks and other vulnerabilities regardless of isolation mode.

## Supported versions

Only the latest release receives fixes. There is no backport line.
