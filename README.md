# InsightSQL Pro — Overnight MVP

A local, explainable SQL analytics prototype with approved natural-language question templates, SQLite read-only query execution, SQLGlot validation, result charts, and query plans.

## Run

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

## Scope and limitations

- This version uses five predefined natural-language questions; it **does not** yet generate arbitrary SQL with an LLM.
- SQL is restricted to a SELECT over the demo `sales` table, executed on a read-only SQLite connection with an authorizer, a progress budget, and an outer row limit.
- This is a learning prototype, not a hardened multi-tenant production SQL gateway.
- Next steps: add schema-aware text-to-SQL with a hosted/local model, more robust query validation and resource isolation, question/SQL benchmark tests, and an explicit explanation engine.
