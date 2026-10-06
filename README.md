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

## Pipeline de análisis

El agente ejecuta un grafo LangGraph:

```
mcp_retrieve → [rag_retrieve] → classify_runtime → 6 expertos en paralelo → merge → END
```

- **`classify_runtime`** etiqueta cada archivo con uno o más runtimes (`browser`, `server`,
  `mobile`, `infra`, `test`, `config`, `unknown`) de forma determinista (path, extensión, imports y
  perfil del proyecto). Cada experto selecciona archivos por intersección de runtimes; no hay
  fallback "analiza todo".
- **Expertos**: los archivos grandes se parten en chunks con solapamiento (nunca se truncan) y los
  chunks se agrupan en lotes acotados; una llamada LLM por lote, en paralelo bajo un semáforo y con
  reintentos. Un lote que falla queda registrado en `failed_batches`.
- **`merge`**: consolidación en dos niveles. L1 determinista por `(path, line, category, code)`;
  L2 con LLM por archivo, exigiendo `source_ids` por issue y rechazando cualquier grupo que pierda,
  invente o rebaje un hallazgo. Si hubo lotes fallidos el resultado incluye `incomplete` y el
  status nunca es `COMPLETED`; el reporte HTML muestra un banner de análisis incompleto.

### Prompts

El `SystemMessage` de cada experto se compone en tiempo de ejecución como
`prompts/system_prompt.md` (preámbulo común) + `prompts/experts/<experto>.md` (dominio). El
preámbulo define el **security boundary**, las reglas de confianza por `[runtime: …]` (en
`browser`/`mobile` todo valor del código es público, incluidas variables de entorno resueltas en
build), la detección de **autenticación basada en secretos del lado cliente** y la **política de
sospechas**: hallazgos no confirmables se reportan igual con título `Sospecha: …`, severidad
MEDIUM máximo (garantizado por código en `BaseExpertNode`) y una frase `Para confirmar: …` con la
verificación concreta.

### Variables de entorno de los expertos

| Variable | Default | Descripción |
|---|---|---|
| `TITVO_EXPERT_FILE_CAP_CHARS` | `30000` | Tamaño máximo de cada chunk (chars). Archivos mayores se parten. |
| `TITVO_EXPERT_BATCH_BUDGET_CHARS` | `200000` | Presupuesto de chars por lote (una llamada LLM). |
| `TITVO_EXPERT_CHUNK_OVERLAP_CHARS` | `5000` | Solapamiento entre chunks consecutivos. |
| `TITVO_EXPERT_MAX_CONCURRENCY` | `4` | Lotes concurrentes (todos los expertos comparten el límite). |

Valores inválidos o no positivos caen al default. En un repo de 100-500 archivos un fullscan
produce decenas de lotes por experto; ajustar `TITVO_EXPERT_MAX_CONCURRENCY` según los límites del
proveedor LLM.

## Infraestructura

La infraestructura está definida en el directorio `aws/` usando Terragrunt:

- `aws/ecr/` — Repositorio ECR
- `aws/batch/` — Definición de job AWS Batch
- `aws/ssm/lookup/` — Lookup de parámetros SSM compartidos
- `aws/ssm/upsert/` — Publicación de ARNs en SSM

## Despliegue a AWS

El despliegue a AWS se automatiza con el workflow de GitHub Actions
`.github/workflows/deploy-to-aws.yml`, que corre sobre pushes a `main`. Flujo:

1. Quality gates: lint (Ruff) y tests (pytest).
2. Autenticación por **OIDC** (`configure-aws-credentials` con `role-to-assume`).
3. `terragrunt apply` sobre `aws/ecr`.
4. Build y push de la imagen a ECR con tags `:<sha>` y `:latest` (la job definition de AWS Batch
   referencía `${ecr_repository_url}:latest`).
5. `terragrunt run-all apply` sobre `aws/`.

### Secretos requeridos (repo GitHub)

| Secret | Descripción |
|---|---|
| `AWS_TITVO_BATCH_ROLE_TO_ASSUME` | ARN del rol IAM OIDC con permisos de deploy sobre `aws/*` (ECR, Batch, SSM, IAM) |
| `AWS_TITVO_ACCOUNT_ID` | ID de la cuenta AWS (p. ej. `895649849416`) |

### Variables del workflow

| Variable | Valor |
|---|---|
| `AWS_REGION` | `us-east-2` |
| `AWS_STAGE` | `prod` |
| `ECR_REPOSITORY` | `tvo-agent-ecr-prod` |
| `IMAGE_TAG` | `${{ github.sha }}` |

`serverless.hcl` monta `AWS_REGION`, `AWS_STAGE` y `AWS_ACCOUNT_ID` vía `get_env`, por lo que el apply
de Terragrunt no requiere variables adicionales. Para reproducir el despliegue localmente:
`AWS_REGION=us-east-2 AWS_STAGE=prod AWS_ACCOUNT_ID=<id> terragrunt run-all apply` dentro de `aws/`.
