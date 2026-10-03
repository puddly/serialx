# Setup
All development dependencies are listed in `pyproject.toml`. To install them, use:
```bash
uv pip install '.[dev]'
```

To include docs tooling as well:
```bash
uv pip install '.[dev,docs]'
```

On macOS and Windows, a Rust toolchain is required to build the native serial port
enumeration extension. Install Rust via [rustup](https://rustup.rs/).

Set up pre-commit hooks with `prek install`. Your code will then be type checked
and auto-formatted when you run `git commit`. You can do this on-demand with
`prek run`.

Serialx relies on automated testing. CI runs tests using both `socat` virtual PTYs
(Linux/macOS) and socket-based serial pairs. To also test with physical adapter pairs,
pass CLI flags to `pytest`:

```bash
pytest --adapter-pair=/dev/serial/by-id/left1,/dev/serial/by-id/right1 \
       --adapter-pair=/dev/serial/by-id/left2,/dev/serial/by-id/right2
```

Each test runs once, on any one of the pairs. More pairs let more `pytest-xdist`
workers run adapter tests at once.

By default, tests run in parallel. You can disable this by passing `-n 0` to `pytest`.
