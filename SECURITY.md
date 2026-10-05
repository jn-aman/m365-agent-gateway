# Security Policy

Report vulnerabilities privately to repository owner. Never post tokens, browser
state, private client configuration, corporate prompts, or token-bearing URLs.

Trusted: OS account, unlocked credential store, installed client, authorized login.
Untrusted: prompts, model calls, retrieved work/web content, tool results, schemas,
JSON requests, and network response shapes.

There is no API key authentication. The local service relies on a loopback bind,
a Host allowlist, and rejection of any request carrying an Origin header, which
blocks DNS rebinding and browser CSRF. It also bounds bodies and context, applies
a local rate limit and deadlines, sanitizes errors, and verifies TLS. Non-loopback
binds are refused outside the container, where the port must be published on host
loopback only. It does not protect against other processes or users on the same
machine: any local client that can reach the port can use your session. Generated
client files are private and contain only a placeholder key. Never enable debug URL
logging or local-variable traceback capture. Upstream WebSocket token is in query
as required by web protocol.

Tokens and browser state use explicit macOS Keychain or Linux Secret Service.
No plaintext fallback, with one exception: `export-session` and `docker-up` write
the live bearer token to a 0600 plaintext file for the Docker container, which
`logout` deletes. A running container keeps its session until stopped. Logout
clears local authentication, not cloud sessions.

Tool validation is not authorization. Clients must enforce human approvals,
filesystem/process/network boundaries, and sandboxing. Schema-valid calls can
still be malicious due to prompt injection. No gateway tool execution exists.

Pinned dependencies and immutable CI action references reduce supply-chain drift.
Browser binaries and undocumented Microsoft protocols remain separate risks.
Live account tests never run in public CI.
