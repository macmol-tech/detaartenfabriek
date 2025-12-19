# de taartenfabriek

A local-first web interface for managing Tart VMs on Apple Silicon macOS.

## Features

- List all local Tart VMs with status and IP
- Browse available macOS images from Cirrus Labs via GitHub API
- Pull new VMs from OCI registries (GitHub Container Registry)
- Clone VMs with optional auto-start
- Create new VMs from base images with custom CPU/memory/disk settings
- Start VMs in VNC mode (`--vnc --no-graphics`) and show a clickable `vnc://` link
- Stop VMs using `tart stop`
- Real-time task logging with WebSocket support
- Card + list views for browsing VMs
- VM categorization (base images vs working VMs)
- VM configuration details via `tart get --format json`
- GitHub token management for API access
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

## GitHub Token Configuration (Optional)

To browse and pull available macOS images from Cirrus Labs, you need to configure a GitHub personal access token:

1. Create a GitHub personal access token with `read:packages` scope:
   - Go to [GitHub Settings > Developer settings > Personal access tokens](https://github.com/settings/tokens)
   - Generate a new token (classic) with the `read:packages` permission

2. Configure the token via the UI:
   - Open the web interface at [http://localhost:8000](http://localhost:8000)
   - Navigate to Settings
   - Enter your GitHub token

3. Or configure it manually:

   ```bash
   echo "your_github_token_here" > ~/.tartvm-manager/github_token
   chmod 600 ~/.tartvm-manager/github_token
   ```

Once configured, the UI will display available macOS images from the Cirrus Labs registry that you can pull directly.

## API

See [API.md](./API.md) for endpoint documentation.

## Security

- The API runs on 127.0.0.1 only
- A random API token is generated on first run
- Token is stored in `~/.tartvm-manager/token` with 0600 permissions (legacy `.token` is migrated if present)
- All API requests require the `X-Local-Token` header
- GitHub token (if configured) is stored in `~/.tartvm-manager/github_token` with 0600 permissions

## Development

- Backend: FastAPI (Python 3.11+)
- Frontend: Vanilla JS + HTML
- Task management: Background processes with logging

## License

MIT
