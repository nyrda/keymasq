# Security Policy

Keymasq includes a privileged daemon, a per-user session broker, and a root
helper for hardware jobs. Security reports should be handled privately.

## Reporting A Vulnerability

Do not open a public issue for security-sensitive bugs.

Report vulnerabilities privately by email to `nyrda@keymasq.tools`.

If GitHub private security advisories are enabled for this repository, you can
also use that channel.

Include:

- affected version or commit
- impacted component
- reproduction steps
- expected impact
- whether the issue requires local access, device access, or code running as
  the desktop user

## Scope

Keymasq does not protect the desktop user's input from unconfined code running
as that user, which already controls the user's Keymasq configuration. Issues
that require such code are out of scope unless they reach another user or gain
privileges.

Security-sensitive areas include:

- commands, input, or stored recordings crossing between local users,
  including daemon ownership and socket authorization
- sandboxed apps reaching the session or daemon sockets
- privilege gained through `keymasqd`, `keymasq-helper`, hardware jobs, or the
  HID-BPF handoff
- service packaging, udev and polkit rules, and runtime permissions

## Hardening And Design Notes

The detailed security model lives in [docs/security.md](docs/security.md).
