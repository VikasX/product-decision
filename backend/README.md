# AI Product Review Intelligence Agent

FastAPI with async PyMongo and MongoDB. Requires Python 3.12+ with pip and venv, plus a local MongoDB server or MongoDB Atlas cluster.

## Setup (PowerShell)

From the repository root:

```powershell
cd backend
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Skip copying the example if you already configured your local .env.

Set MONGODB_URI in .env to your MongoDB connection string and MONGODB_DATABASE to product_reviews. Credentials belong in this ignored file. Environment variables override .env.

For Atlas, choose Connect > Drivers > Python > PyMongo (Asynchronous). Copy the URI and replace the password placeholder with your database user's URL-encoded password. Allow your current public IP under Database & Network Access, and give the database user readWrite access to product_reviews. Wait for the cluster to finish provisioning.

MONGODB_TIMEOUT_MS defaults to 5000 and bounds database operations. CORS_ORIGINS is a JSON array of exact origins such as ["http://localhost:3000"], defaulting to no cross-origin access. LOG_LEVEL defaults to INFO.

## Connection test and server

Run from backend:

```powershell
.\.venv\Scripts\python -m app.db.check
.\.venv\Scripts\python -m uvicorn app.main:create_app --factory --reload
```

The connection command pings MongoDB, returns exit code 0 on success and 1 on failure, and does not print credentials. Check the URI, database credentials, Atlas IP access, DNS and cluster status if it fails.

Open [Swagger](http://127.0.0.1:8000/docs). GET /api/v1/health reports application liveness. GET /api/v1/health/db pings MongoDB and returns 503 when unavailable. The application creates one client per lifespan and closes it at shutdown. Future routes can use get_database from app.db.session as a FastAPI dependency.

No collections are created by these checks. MongoDB creates the database and collections on the first write. There are currently no data migrations. Add explicit index setup and versioned data migration scripts when application data requires them.

## Developer checks

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\ruff check .
.\.venv\Scripts\ruff format --check .
```

Unit tests mock database responses and run without Atlas. Use app.db.check to verify the actual deployment.
