# Contributing

Use Python 3.11+ on macOS or Linux. Run locked development install, lint, formatter
check, and tests before proposing changes. Add regression tests for protocol,
streaming, session capture, schema, and auth changes.

Fixtures must be synthetic. Never include real tokens, corporate prompts, captures,
browser state, or private client files. Do not weaken TLS, keyring-only storage,
host checks, client approvals, or unsupported-feature errors. Keep offline protocol
proof separate from live model reliability.
