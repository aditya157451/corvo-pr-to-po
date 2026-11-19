# APPROACH.md

**AI tools used**
- **Claude (Anthropic)** via the Claude Code/agent environment: pair-programmed the engine, API, UI and tests; I reviewed the logic and checked it against the 14 worked examples and the three preferred-supplier tables.
- **Inside the app (optional, needs `ANTHROPIC_API_KEY`)**: Claude reads policy amendments into a structured patch, and extracts slots from chat messages. Both have deterministic fallbacks, so the app works with no key. Item matching is never done by the LLM: the assistant can only choose real item-master items.

**Method:** engine first (pure Python, no I/O), validated against worked examples, then storage, API, UI. 20 automated tests (`pytest`).
