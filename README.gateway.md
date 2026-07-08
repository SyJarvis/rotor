# Rotor

A FastAPI-based API aggregation gateway that provides unified access to multiple AI model providers (OpenAI, Anthropic, Moonshot/Kimi, MiniMax, Zhipu/BigModel) with automatic protocol conversion, load balancing, and failover.

## Features

- **Multi-Provider Support**: OpenAI, Anthropic, Moonshot, MiniMax, Zhipu
- **Protocol Conversion**: Seamless OpenAI <-> Anthropic protocol translation
- **Load Balancing**: Priority-based with weighted random selection
- **Automatic Failover**: Retry logic with exponential backoff
- **Token Management**: API key authentication with quota tracking
- **Request Logging**: Complete audit trail of all requests
- **Streaming Support**: SSE streaming for real-time responses
- **Admin API**: Full CRUD for channels, tokens, and logs

## Quick Start

### Installation

```bash
# Clone the repository
git clone <repo-url>
cd rotor

# Install dependencies
source .venv/bin/activate
uv pip install -r requirements.txt
pip install -r requirements.txt

# Copy environment file
cp .env.example .env

# Edit .env with your configuration
```

### Running

```bash
# Development mode
uvicorn rotor.main:app --reload --port 8000

# Production mode
uvicorn rotor.main:app --host 0.0.0.0 --port 8000 --workers 4
```

### Docker

```bash
# Build and run
docker-compose up -d
```

## Configuration

Create a `.env` file with the following:

```env
DATABASE_URL=sqlite+aiosqlite:///./data/rotor.db
SECRET_KEY=your-secret-key
API_KEY_PREFIX=sk-
LOG_LEVEL=INFO
```

## Usage

### 1. Create a Channel

```bash
curl -X POST http://localhost:8000/api/admin/channels \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Moonshot",
    "type": "moonshot",
    "key": "your-api-key",
    "base_url": "https://api.moonshot.cn/v1",
    "models": ["moonshot-v1-8k", "moonshot-v1-32k"],
    "priority": 1,
    "weight": 1,
    "enabled": true
  }'
```

### 2. Create a Token

```bash
curl -X POST http://localhost:8000/api/admin/tokens/generate \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Token",
    "quota": 1000000
  }'
```

### 3. Make API Requests

OpenAI Protocol:
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "moonshot-v1-8k",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```
http://localhost:8000/anthropic/v1/messages
## API Endpoints

### OpenAI Compatible

- `POST /v1/chat/completions` - Chat completions
- `GET /v1/models` - List available models
http://localhost:8000/v1/chat/completions
### Admin API

- `GET /api/admin/channels` - List channels
- `POST /api/admin/channels` - Create channel
- `PUT /api/admin/channels/{id}` - Update channel
- `DELETE /api/admin/channels/{id}` - Delete channel

- `GET /api/admin/tokens` - List tokens
- `POST /api/admin/tokens/generate` - Generate token
- `PUT /api/admin/tokens/{id}` - Update token
- `DELETE /api/admin/tokens/{id}` - Delete token

- `GET /api/admin/logs` - View request logs
- `GET /api/admin/logs/stats` - Usage statistics

## Supported Providers

| Provider | Type | Base URL | Protocol |
|----------|------|----------|----------|
| Moonshot | `moonshot` | `https://api.moonshot.cn/v1` | OpenAI |
| MiniMax | `minimax` | `https://api.minimaxi.com/v1` | Anthropic |
| Zhipu | `zhipu` | `https://open.bigmodel.cn/api/anthropic` | Anthropic |
| OpenAI | `openai` | `https://api.openai.com/v1` | OpenAI |
| Anthropic | `anthropic` | `https://api.anthropic.com/v1` | Anthropic |

## Project Structure

```
rotor/
├── app/
│   ├── main.py              # Application entry
│   ├── config.py            # Configuration
│   ├── database.py          # Database connection
│   ├── core/                # Core modules
│   │   ├── security.py      # Authentication
│   │   ├── exceptions.py    # Custom exceptions
│   │   ├── middleware.py    # Middleware
│   │   └── deps.py          # Dependencies
│   ├── models/              # SQLAlchemy models
│   ├── schemas/             # Pydantic schemas
│   ├── adapters/            # Provider adapters
│   │   ├── base.py          # Base adapter
│   │   ├── factory.py       # Adapter factory
│   │   ├── protocol/        # Protocol converters
│   │   └── providers/       # Provider implementations
│   ├── services/            # Business logic
│   │   ├── loadbalancer.py  # Load balancing
│   │   └── retry.py         # Retry logic
│   └── api/                 # API routes
│       ├── v1/              # OpenAI compatible
│       └── admin/           # Admin endpoints
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

## License

MIT
