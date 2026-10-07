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


## Archivos CLI y batching

`CliSnapshotRepository` recupera tar.gz registrados en DynamoDB por `batch_id`
(`batch_id_gsi`). `main.py` usa `CliRetrievalNode` para tareas CLI, sin tools MCP ni
RAG Git. Configurar `TITVO_DYNAMO_CLI_FILES_TABLE_NAME` y
`TITVO_DYNAMO_CLI_FILES_BUCKET_NAME` (o configuración `cli_files_bucket_name`).
Los paquetes se leen sin extracción y se verifica el manifiesto opcional.

Los expertos particionan archivos grandes con solapamiento y contexto
estructural, manteniendo las líneas originales. `TITVO_EXPERT_FILE_CAP_CHARS`
(default 30000) controla la partición y `TITVO_EXPERT_BATCH_BUDGET_CHARS`
(200000) acota mensajes contando prompt, encabezados, RAG y reserva. Los
valores son caracteres, no tokens. Se conserva la clasificación runtime y el
fan-out de expertos de main, con concurrencia de lotes acotada por configuración.
Errores de invocación o parseo quedan en `coverage.errors`; cobertura parcial
no se presenta como ejecución completa aunque el estado de seguridad sea WARNING.

Consolidación por archivo y grupos acotados, con `source_ids` para no omitir
hallazgos. El laboratorio MiniStack y el cliente están en el repo titvo-dev.

### Recuperación de respuestas de expertos

Todos los expertos reciben un contrato común de salida JSON con rutas exactas
del lote y líneas enteras positivas. La validación normaliza `./` inicial,
separadores de ruta y líneas numéricas representadas como strings; no adivina
rutas ni acepta traversal, archivos fuera del lote o líneas fuera del archivo.

Para hallazgos JSON con campos inválidos se solicita una sola corrección por
lote usando `source_id`. Los hallazgos inicialmente válidos se conservan. Una
corrección no puede cambiar el título ni la evidencia existente, mover un
hallazgo de una ruta válida o bajar su severidad original. Si debe completar
evidencia o cambiar una ruta inválida, el código debe existir literalmente en
el archivo de destino. Cada registro rechazado debe quedar contabilizado;
omitirlo, inventar IDs o devolver una lista vacía mantiene cobertura incompleta.
Una corrección parcial conserva los hallazgos corregidos y los errores restantes.

La entrada de corrección tiene un máximo de 16000 caracteres de registros
rechazados y el mensaje completo respeta el presupuesto del lote. Los fallos
transitorios usan hasta tres intentos de transporte por solicitud. No se
repiten expertos o lotes ya procesados dentro de esta ejecución. No se implementa
reanudación de tareas anteriores: un nuevo scan inicia un análisis nuevo.
Respuestas con JSON ilegible o envelope inválido conservan un error explícito;
no se intenta inventar hallazgos a partir de ese texto.

`coverage.experts` incluye `repair_attempts`, `batches_repaired` y
`batch_diagnostics` con números de lote desde 1, motivos originales y resultado.
No se serializa el texto crudo rechazado. Cambios de prompts/validación requieren
reconstruir la imagen del Agent antes de un nuevo análisis.


### Resumen medido en AWS

El resultado en DynamoDB incorpora `metrics` (tiempo y lotes), `usage` (llamadas,
tokens y costo estimado) e `issues_count`. Se conserva el reporte HTML y las
notificaciones existentes, sin agregar esos campos al DTO de notificaciones.
El costo cubre llamadas al modelo y no embeddings, impuestos ni servicios AWS.
Sin consumo/tarifa disponible, no se inventa un costo. Los endpoints proxy no
usan tarifas estándar automáticamente. Las tarifas pueden configurarse con
`TITVO_PRICE_INPUT_PER_MILLION`, `TITVO_PRICE_CACHED_PER_MILLION` y
`TITVO_PRICE_OUTPUT_PER_MILLION` (USD por millón de tokens).

La definición AWS Batch añade bucket/tabla CLI mediante los parámetros SSM
existentes. Las tareas Git conservan MCP/RAG y el pipeline `main` de despliegue
continúa igual. Integrar esta rama y `titvo-admin-bff-aws/codex/scan-execution-summary`
antes de `titvo-admin-web/codex/scan-dashboard`. Para probar sin desplegar AWS,
consultar `titvo-dev/tools/cli/README.md` y sus ramas de laboratorio.

Validación: 345 pruebas unitarias con Python 3.13 y `uv.lock` en Docker;
MiniStack comprueba además snapshots, múltiples lotes y rechazo de paquetes
corruptos sin llamadas a modelos reales. No se ha desplegado esta rama en AWS.
