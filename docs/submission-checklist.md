# Submission handoff

The repository includes the application, migrations, synthetic fixtures, tests, Docker setup, provider checks and documentation. Start with the README to run the project, then follow the walkthrough for the feedback demo.

## Included

- [README](../README.md): one-command startup, Mock/real configuration, context anatomy and API routes.
- [Architecture](architecture.md): harness design, memory selection, regional isolation, claims, retries and trade-offs.
- [Walkthrough](walkthrough.md): repeatable before/after feedback demonstrations and the main code paths.
- [Verification](verification.md): completed checks and provider results.
- [AI assistance](ai-assistance.md): design decisions, implementation review and assistance used.
- [Exact context example](example-context.json): recorded Mock inputs and outputs before and after approved feedback.
- [Claude feedback example](anthropic-feedback.json): recorded inputs, outputs, guidance and approval from the live API.

## Demo sequence

Follow the initial verdict, supervisor correction, proposed rule, approval, and later review containing that rule. The one-day entry exception dismisses the **Same day** follow-up and asks for authorization on **Two days later**. The Mock severity example demonstrates the feedback loop offline. Both are described in the [walkthrough](walkthrough.md).

The [Loom](https://www.loom.com/share/253ef7d8a36a49eba6e7cbc559d3cd96) uses Codex; the [recorded Claude feedback loop](anthropic-feedback.json) shows the same flow through Claude's SDK.
