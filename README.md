# de taartenfabriek

A local-first web interface for managing Tart VMs on Apple Silicon macOS.

## Features

- List all local Tart VMs with status and IP
- Start VMs in VNC mode (`--vnc --no-graphics`) and show a clickable `vnc://` link
- Stop VMs using `tart stop`
- Pull new VMs from OCI registries
- Real-time task logging
- Card + list views for browsing VMs
- VM configuration details via `tart get --format json`
- Simple, local-first design

## Prerequisites

- macOS 13+ (Ventura, Sonoma, or newer)
- Python 3.11+
- [Tart](https://github.com/cirruslabs/tart) installed and in PATH

## Getting Started

1. Clone this repository
2. Install dependencies:

   ```bash
   make install
   ```

3. Start the development server:

   ```bash
   make dev
   ```

4. Open [http://localhost:8000](http://localhost:8000) in your browser

## API

See [API.md](./API.md) for endpoint documentation.

## Security

- The API runs on 127.0.0.1 only
- A random API token is generated on first run
- Token is stored in `~/.tartvm-manager/token` with 0600 permissions (legacy `.token` is migrated if present)
- All API requests require the `X-Local-Token` header

## Development

- Backend: FastAPI (Python 3.11+)
- Frontend: Vanilla JS + HTML
- Task management: Background processes with logging

## License

MIT
