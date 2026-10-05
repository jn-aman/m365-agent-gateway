# Security Policy

Report vulnerabilities privately to repository owner. Never post tokens, browser
state, private client configuration, corporate prompts, or token-bearing URLs.

Trusted: OS account, unlocked credential store, installed client, authorized login.
Untrusted: prompts, model calls, retrieved work/web content, tool results, schemas,
JSON requests, and network response shapes.

Local service uses key authentication, host/Origin checks, bounded bodies/context,
rate limits, deadlines, sanitized errors, and verified TLS. It does not isolate
against malicious processes running as the same OS user. Generated client files
are private but contain credentials. Never enable debug URL logging or local-variable
traceback capture. Upstream WebSocket token is in query as required by web protocol.

Tokens and browser state use explicit macOS Keychain or Linux Secret Service.
No plaintext fallback. Logout clears local authentication, not cloud sessions.

Tool validation is not authorization. Clients must enforce human approvals,
filesystem/process/network boundaries, and sandboxing. Schema-valid calls can
still be malicious due to prompt injection. No gateway tool execution exists.

Pinned dependencies and immutable CI action references reduce supply-chain drift.
Browser binaries and undocumented Microsoft protocols remain separate risks.
Live account tests never run in public CI.
