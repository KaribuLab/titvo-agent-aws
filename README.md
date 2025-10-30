# Titvo Agent Gateway

## Development

```bash
uv venv --python 3.13
source .venv/bin/activate
uv sync
```

## Run

```bash
LOG_LEVEL=DEBUG
TASK_ID=<task_id>
TASK_TABLE_NAME=<task_table_name>
PARAMETERS_TABLE_NAME=<parameters_table_name>
ENCRYPTION_KEY_NAME=<encryption_key_name>
MCP_SERVER_URL=<mcp_server_url>
SYSTEM_PROMPT=<system_prompt>
CONTENT_TEMPLATE=<content_template>
IA_PROVIDER=<ia_provider>
IA_MODEL=<ia_model>
IA_API_KEY=<ia_api_key>
# Localstack
AWS_ENDPOINT=http://localhost:4566
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=dummy
AWS_SECRET_ACCESS_KEY=dummy
python src/main.py
```