---
title: sqlbot
emoji: 🧠
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
---

## sqlbot — AI SQL Analyst

This Space runs a FastAPI app that lets you ask natural-language questions about your PostgreSQL database and get:

- Generated SQL
- Executed query results
- Explanations and insights

### Deployment notes

- The backend FastAPI app is defined in `app.py`.
- The Docker image is built from `Dockerfile` and exposes port `7860`.
- The app connects to PostgreSQL via the `DATABASE_URL` environment variable (configured in the Space settings, e.g., a Neon connection string).

