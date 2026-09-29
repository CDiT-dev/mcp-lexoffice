## ADDED Requirements

### Requirement: FastMCP 3 streamable-http transport
The server SHALL use FastMCP 3.x with `streamable-http` transport and `json_response=True` on port 8000.

#### Scenario: Server starts with streamable-http
- **WHEN** the server process starts
- **THEN** it listens on `0.0.0.0:8000` using streamable-http transport with JSON responses enabled

#### Scenario: Claude.ai connector initialization
- **WHEN** a Claude.ai custom connector sends a GET request to the MCP endpoint
- **THEN** the server responds with valid JSON (not SSE stream) and the connector initializes successfully

### Requirement: Bearer token from environment variable
The server SHALL load `LEXOFFICE_API_KEY` from environment variables (with `.env` file support via python-dotenv) at startup.

#### Scenario: API key from environment variable
- **WHEN** `LEXOFFICE_API_KEY` is set as an environment variable
- **THEN** the server uses it as the Bearer token for all Lexoffice API requests

#### Scenario: API key from .env file
- **WHEN** `LEXOFFICE_API_KEY` is not set as an environment variable but exists in `.env`
- **THEN** python-dotenv loads it and the server uses it as the Bearer token

#### Scenario: 1Password reference fallback
- **WHEN** `LEXOFFICE_API_KEY` value starts with `op://`
- **THEN** the server resolves it via 1Password CLI (`op read`) and uses the result as the Bearer token

#### Scenario: Missing API key
- **WHEN** `LEXOFFICE_API_KEY` is not set and no `.env` file exists
- **THEN** the server raises a RuntimeError at startup with a descriptive message

### Requirement: Rate limit handling with retry
The server SHALL respect the Lexoffice 2 req/s rate limit and handle HTTP 429 responses with retry.

#### Scenario: Rate limit throttling
- **WHEN** the server makes API requests
- **THEN** it limits concurrent requests to 2 via semaphore

#### Scenario: HTTP 429 retry
- **WHEN** the Lexoffice API returns HTTP 429
- **THEN** the client waits for the duration specified in the Retry-After header (or 1 second if absent) and retries the request
