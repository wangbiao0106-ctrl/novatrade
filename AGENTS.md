# Repository Working Rules

The authoritative directory layout is defined in [`spec/DIRECTORY_STRUCTURE.md`](spec/DIRECTORY_STRUCTURE.md).

Keep raw cryptocurrency candles and their collection metadata under `data/kline/`. Keep strategy research, temporary implementations, configurations, tests, and results inside one dedicated `strategies/<strategy_name>/` directory. Runtime Swift implementations belong under `Sources/`; they must not import files from `data/` or `strategies/` at runtime.

When moving or adding a strategy artifact, update the strategy directory README and every command or documentation reference in the same change. Do not add reports, parameter grids, symbol lists, or derived arrays to `data/kline/`.
