# InsightSQL Pro

### AI-Powered Natural Language SQL Analytics Platform

InsightSQL Pro is an interactive data analytics application that helps users explore relational data using natural-language questions instead of manually writing SQL queries.

Built with Python and Streamlit, it combines local AI-powered SQL generation, database exploration, query validation, interactive visualizations, and SQL performance insights in a unified interface.

## Features

- **Natural Language to SQL:** Generate SQL queries from plain-English questions using a locally running Ollama language model.
- **Database Uploads:** Import SQLite databases, CSV files, and Excel spreadsheets for analysis.
- **Automatic Schema Discovery:** Explore available tables and database structures.
- **SQL Validation:** Use SQLGlot and read-only execution safeguards to restrict potentially unsafe queries.
- **Interactive Dashboards:** Explore query results through Plotly visualizations.
- **Query Execution Plans:** Inspect SQLite query plans to better understand database operations.
- **Query Optimization Insights:** View basic suggestions for improving SQL queries.
- **User Authentication:** Sign up and log in through a local authentication system with salted password hashing.
- **Data Export:** Download query results as CSV files.

## Technology Stack

| Category | Technologies |
|---|---|
| Programming Language | Python |
| Frontend | Streamlit |
| AI Integration | Ollama, Qwen2.5-Coder |
| Database | SQLite |
| SQL Parsing and Validation | SQLGlot |
| Data Processing | Pandas |
| Data Visualization | Plotly |
| Authentication | SQLite, PBKDF2 password hashing |

## How It Works

1. The user signs in to the application.
2. The user uploads a supported database or data file.
3. InsightSQL Pro discovers the available tables and schema.
4. The user asks a question in natural language.
5. The local language model generates a SQL query using the available schema.
6. The application validates the generated SQL before execution.
7. The query executes against the selected database using read-only safeguards.
8. The results are displayed in a table, with available charts and query insights.

## Installation and Setup

### Prerequisites

- Python
- Git
- Ollama installed locally

### 1. Clone the Repository

```bash
git clone https://github.com/MrunaliSawant06/InsightSQL---Pro.git
cd InsightSQL---Pro
```

### 2. Create a Virtual Environment

```bash
python -m venv .venv
```

Activate it on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Set Up Ollama

Install Ollama from https://ollama.com and download the language model:

```bash
ollama pull qwen2.5-coder:1.5b
```

Ensure the Ollama service is running before using AI-powered SQL generation.

### 5. Launch the Application

```bash
streamlit run app.py
```

Open the local URL shown in your terminal, typically:

http://localhost:8501

## Security Considerations

InsightSQL Pro includes SQL validation and read-only query execution safeguards to reduce the risk of unsafe database operations.

User passwords are stored as salted password hashes rather than plaintext.

The project is intended for learning, experimentation, and portfolio demonstration. It has not been independently security-audited and should not be treated as a production-ready, multi-tenant database platform.

## Current Limitations

- AI-powered SQL generation requires a locally running Ollama model.
- Query generation accuracy depends on the model and database schema.
- The application currently focuses on SQLite-backed analytics.
- Advanced deployment security, resource isolation, and comprehensive automated testing remain future improvements.

## Future Enhancements

- Support for PostgreSQL and MySQL.
- More advanced dashboard generation.
- SQL query performance benchmarking.
- Automated natural-language-to-SQL evaluation.
- Improved error recovery and query explanations.
- Cloud deployment with configurable AI model providers.

## Author

**Mrunali Sawant**

B.Tech — Data Science

GitHub: https://github.com/MrunaliSawant06

---

*InsightSQL Pro — Making database exploration more accessible through natural language and interactive analytics.*