# AI Product Review Intelligence Agent

The FastAPI backend exposes a versioned liveness endpoint. Install Python 3.12 or newer with pip and venv. For database work, provision a PostgreSQL database and a role with permission to create tables in its target schema.

## Local setup (PowerShell)

From the repository root:

```powershell
cd backend
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Edit `.env` in this directory. Set `DATABASE_URL` to your PostgreSQL connection string using the `postgresql+asyncpg` driver. Supply your own role, password if needed, host, port and database name. Percent-encode special characters in credentials. Operating system environment variables override the file.

`CORS_ORIGINS` is a JSON array of exact browser origins, for example `["http://localhost:5173"]`. The default empty array grants no cross-origin access. `LOG_LEVEL` defaults to `INFO`.

## Run

Run all commands from the backend directory:

```powershell
.\.venv\Scripts\python -m uvicorn app.main:create_app --factory --reload
```

Open [Swagger UI](http://127.0.0.1:8000/docs) or [health](http://127.0.0.1:8000/api/v1/health). Health returns `{"status":"ok"}` and checks application liveness independently of PostgreSQL availability.

## Checks

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\ruff check .
.\.venv\Scripts\ruff format --check .
```

## Migrations

With the configured database running, apply pending revisions:

```powershell
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\alembic current
```

The initial project has no revisions or application tables. When adding models, inherit from `app.db.base.Base` and import their modules in `alembic/env.py` so Alembic sees the metadata. Generate a revision with `.\.venv\Scripts\alembic revision --autogenerate -m "describe the change"`, review its upgrade and downgrade operations, then run `upgrade head`.
