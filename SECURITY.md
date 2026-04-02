# Security Policy

Keyforge includes a privileged daemon, session broker, and protected input
recording paths. Security reports should be handled privately.

## Reporting A Vulnerability

Do not open a public issue for security-sensitive bugs.

Until a dedicated security mailbox is published, use GitHub private security
advisories for this repository if available. If private advisories are not
enabled yet, contact the maintainer directly through the project contact listed
in the repository profile and request a private reporting channel.

Include:

- affected version or commit
- impacted component
- reproduction steps
- expected impact
- whether the issue requires local access, device access, or an unlocked session

## Scope

Security-sensitive areas include:

- daemon and session socket authorization
- recording unlock and owner-binding
- macro recording and playback boundaries
- combo capture and original-input observation
- polkit helper path pinning
- service packaging, udev rules, and runtime permissions

## Hardening And Design Notes

The detailed security model lives in [docs/SECURITY.md](docs/SECURITY.md).
