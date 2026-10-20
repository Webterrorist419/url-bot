# Contributing

Use Python 3.10 or newer. The bot has no third-party runtime dependencies.
Copy `config.env.example` to `config.env` for local use. Keep credentials,
SQLite files and generated binaries out of commits.

Run the bot tests without a Telegram connection:

```bash
python3 -B -m unittest -v test_broadcast test_bot
```

On Linux, install `requirements-build.txt` into a virtual environment and run:

```bash
python3 packaging/build_release.py
python3 -B packaging/test_package.py -v
```

Describe behavior changes and the checks you ran in pull requests. Keep the
Russian and English bot messages aligned. Do not change existing SQLite
settings or callback identifiers without a compatible migration.

For a release, update `VERSION`, the bot's User-Agent version, and
`CHANGELOG.md`, then create a matching `vMAJOR.MINOR.PATCH` tag. The GitHub
workflow builds the binary from the checked-in source, runs tests and attaches
the Debian package and checksums to the tag's release.
