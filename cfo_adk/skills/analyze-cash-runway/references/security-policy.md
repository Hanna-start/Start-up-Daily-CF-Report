# Security Policy

- Accept local CSV input only.
- Do not send raw bank rows, account identifiers, or original transaction descriptions to the language model.
- Use only aggregated and redacted calculation summaries in prompts and language-model-visible state.
- Keep API keys in environment variables and exclude local approvals and generated reports from version control.
- Require human approval before uncertain classifications or recurring candidates affect the forecast.
- Treat model-generated prose as commentary. Python calculation output remains the source of truth.
