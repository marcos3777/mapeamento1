# Backend – YouOn (API + Worker)

FastAPI (Python) para expor as APIs e um worker Node/TypeScript para baixar/extrair os arquivos e atualizar o status no banco.

## Visão geral

- **API (FastAPI)** – `./api`
  - Endpoints:
    - `GET /v1/ping`
    - `POST /v1/importar`
    - `GET /v1/import-status`
- **Worker (Node/TS)** – `./worker/src/importWorker.ts`
  - Lê jobs `pending` em `import_status`, baixa/extrai dados e atualiza o progresso em `observacoes` + `status`.

## Requisitos

- Python 3.10+ (recomendado 3.11)
- Node.js 18+ (ou 20+)
- PostgreSQL acessível via rede
- Git

## 1) Configurar e rodar a **API**

### 1.1 Criar e ativar venv (Windows / PowerShell)
```powershell
cd api
python -m venv .venv
.\.venv\Scripts\activate

cd api
python3 -m venv .venv
source .venv/bin/activate

pip install -r ../requirements.txt

# Obrigatórios
DATABASE_URL=postgresql+asyncpg://USUARIO:SENHA@HOST:5432/NOME_DB?ssl=require
DB_SCHEMA=intel_lead

# CORS (frontend local)
ALLOW_ORIGINS=http://localhost:3000

# Rodar a API
uvicorn app.main:app --reload --port 8000
