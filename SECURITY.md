# Security

Report a vulnerability privately through GitHub's **Report a vulnerability** button
on this repository's Security tab. Please include a minimal synthetic reproduction,
and never paste credentials, real endpoints or private logs into a public issue.

Treat endpoints and model output as untrusted. Trust boundaries: operator-selected
endpoint and image; private config/credential environment; untrusted worker;
protected verifier; allowlisted report/export. Docker engine/image integrity is a
prerequisite. An internal network limits the worker to operator-authorized network
members, not a single-host firewall. Do not put unrelated sensitive services on
that network. No Docker socket or writable host repository is mounted. No host
execution fallback is permitted.

The Python source grammar is specific to the slug fixture; do not repurpose it as
an arbitrary-code sandbox. All candidate execution still requires the container
boundary. Credentials are supported only in direct host requests; the Pi workload
sends none, so it refuses credential-protected endpoints. Redirects and ambient HTTP
proxies are disabled for direct requests to avoid forwarding credentials to
unintended destinations.

Schema validation cannot prove evidence is honest. Results are local observations,
not signed attestations. Independent review must distinguish synthetic, incomplete,
operator-supplied and measured facts. Hashes establish equality, not authorship.
