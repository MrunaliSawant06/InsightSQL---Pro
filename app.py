import sqlite3
import pandas as pd
import plotly.express as px
import streamlit as st
import sqlglot
from sqlglot import exp
import ollama
import time
import io
import hashlib
import re
import tempfile
import os
import html
import hmac
import secrets
import datetime
from pathlib import Path

DB = None  # The database is supplied by the user, not hardcoded.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_IMPORT_ROWS = 100_000

# ---------------------------------
# DATABASE INITIALIZATION
# ---------------------------------
@st.cache_resource
def initialize():
    # No sample database is created. Each upload is handled independently.
    return None

# ---------------------------------
# PREDEFINED SQL TEMPLATES
# ---------------------------------
TEMPLATES = {}  # No fixed sales questions: questions come from the user.

# ---------------------------------
# SQL VALIDATION
# ---------------------------------
def validate_sql(sql, database_schema):
    if not sql or not sql.strip():
        raise ValueError("The SQL query is empty.")

    statements = sqlglot.parse(sql, read="sqlite")
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError("Only one SELECT statement is allowed.")

    tree = statements[0]
    forbidden = (
        exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop,
        exp.Alter, exp.Command, exp.Attach, exp.Pragma,
    )
    if any(isinstance(node, forbidden) for node in tree.walk()):
        raise ValueError("Unsafe SQL operation rejected.")

    # Conservative MVP: a single physical table, no joins, CTEs or subqueries.
    if (tree.find(exp.Join) or tree.find(exp.With) or tree.find(exp.Subquery)
            or tree.find(exp.Union) or tree.find(exp.Intersect) or tree.find(exp.Except)):
        raise ValueError("Complex SQL is not supported in this version.")

    if tree.find(exp.Select) is not tree:
        raise ValueError("Nested SELECT statements are not allowed.")

    if tree.find(exp.Join) or tree.find(exp.With) or tree.find(exp.Subquery) or tree.find(exp.Union):
        raise ValueError("Joins, CTEs, subqueries and unions are not supported yet.")

    tables = list(tree.find_all(exp.Table))
    allowed_tables = {name.casefold() for name in database_schema}
    if len(tables) != 1 or tables[0].name.casefold() not in allowed_tables:
        raise ValueError("Query must use exactly one table from the uploaded database.")

    if tables[0].db or tables[0].catalog:
        raise ValueError("Cross-database references are not allowed.")

    # Block SQLite functions that can have side effects or access files.
    blocked_functions = {"load_extension", "readfile", "writefile", "sqlite_compileoption_get"}
    for fn in tree.find_all(exp.Func):
        name = fn.sql_name().casefold() if hasattr(fn, "sql_name") else ""
        if name in blocked_functions:
            raise ValueError(f"Function {name} is not allowed.")

    return sql.strip().rstrip(";")

# ---------------------------------
# NATURAL LANGUAGE TO SQL
# ---------------------------------
def generate_sql(question):
    # Legacy rule-based generation intentionally disabled for arbitrary schemas.
    return None

@st.cache_data(show_spinner=False, max_entries=100)
def generate_sql_ai(question, schema_items):
    schema_text = ""
    for table_name, columns in schema_items:
        schema_text += f"\nTable: {table_name}\n"
        for column_name, data_type in columns:
            schema_text += f"- {column_name} {data_type or 'UNKNOWN'}\n"

    prompt = f"""You are an expert SQLite query generator.

DATABASE SCHEMA:
{schema_text}

TASK:
Convert the user's question into a valid SQLite SELECT query.

RULES:
1. Return ONLY one SQLite SELECT query, with no markdown or explanation.
2. Use ONLY the listed tables and columns; quote identifiers when needed.
3. For this version, query only ONE table. No JOIN, CTE, UNION or subquery.
4. Never generate statements that change data or schema.
5. Use SQLite-compatible syntax.
6. If the question cannot be answered from the schema, return exactly UNSUPPORTED.
7. Prefer LIMIT 200 for exploratory row listings.

USER QUESTION:
{question}

SQL:"""

    response = ollama.chat(
        model="qwen2.5-coder:1.5b",
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0, "num_predict": 200, "num_ctx": 2048},
        keep_alive="10m",
    )
    generated_sql = response["message"]["content"].strip()
    if generated_sql.upper() == "UNSUPPORTED":
        return None
    if generated_sql.startswith("```"):
        generated_sql = generated_sql.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return generated_sql


def analyze_sql(sql):
    """Basic heuristic suggestions, not verified performance improvements."""
    suggestions = []
    query = sql.upper()
    if "SELECT *" in query:
        suggestions.append("Avoid SELECT * when you only need specific columns.")
    if "WHERE" not in query:
        suggestions.append("For large tables, consider filtering rows when appropriate.")
    if "ORDER BY" in query:
        suggestions.append("ORDER BY may require sorting; inspect the execution plan.")
    if "GROUP BY" in query:
        suggestions.append("GROUP BY performs aggregation; review the execution plan.")
    if "LIMIT" not in query:
        suggestions.append("Consider LIMIT for exploratory queries.")
    if not suggestions:
        suggestions.append("No common optimization opportunities detected.")
    return suggestions

# ---------------------------------
# SAFE QUERY EXECUTION
# ---------------------------------
def open_read_only_connection(database_path):
    # SQLite URI mode=ro avoids modifications to the uploaded copy.
    conn = sqlite3.connect(Path(database_path).as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.execute("PRAGMA query_only=ON")
    conn.enable_load_extension(False)
    conn.set_authorizer(lambda action, a1, a2, db, source: (
        sqlite3.SQLITE_OK if action in (
            sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION
        ) and (action != sqlite3.SQLITE_FUNCTION or str(a2 or a1 or "").lower() not in
               {"load_extension", "readfile", "writefile"})
        else (sqlite3.SQLITE_OK if action == sqlite3.SQLITE_PRAGMA
              and str(a1 or "").lower() == "table_info"
              else sqlite3.SQLITE_DENY)
    ))
    steps = [0]
    started = time.monotonic()
    def progress():
        steps[0] += 1
        return 1 if steps[0] > 3000 or time.monotonic() - started > 5 else 0
    conn.set_progress_handler(progress, 1000)
    return conn


def run_query(sql, database_path, database_schema):
    clean_sql = validate_sql(sql, database_schema)
    ro = open_read_only_connection(database_path)
    try:
        df = pd.read_sql_query(
            f"SELECT * FROM ({clean_sql}) AS limited_result LIMIT 200", ro
        )
        plan = ro.execute("EXPLAIN QUERY PLAN " + clean_sql).fetchall()
        return df, plan
    finally:
        ro.close()

# ---------------------------------
# USER ACCOUNT AUTHENTICATION
# ---------------------------------
# Accounts are persisted on this computer, separately from uploaded datasets.
AUTH_DIR = Path.home() / ".insightsql_pro"
AUTH_DB = AUTH_DIR / "users.db"

def auth_connection():
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    try:
        AUTH_DIR.chmod(0o700)
    except OSError:
        pass
    conn = sqlite3.connect(str(AUTH_DB), timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            salt BLOB NOT NULL,
            password_hash BLOB NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn

def password_digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)

def create_account(username, password):
    username = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise ValueError("Username must be 3–32 characters: letters, numbers, _, . or -.")
    if len(password) < 12 or len(password) > 128:
        raise ValueError("Use a password between 12 and 128 characters.")
    salt = secrets.token_bytes(16)
    digest = password_digest(password, salt)
    with auth_connection() as conn:
        try:
            conn.execute(
                "INSERT INTO users (username, salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (username, salt, digest, datetime.datetime.now(datetime.timezone.utc).isoformat()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("This username is already taken.") from None
    return username

def verify_account(username, password):
    with auth_connection() as conn:
        row = conn.execute(
            "SELECT username, salt, password_hash FROM users WHERE username = ?",
            (username.strip(),),
        ).fetchone()
    # Do comparable work even for an unknown username.
    salt = row[1] if row else b"\0" * 16
    expected = row[2] if row else b"\0" * 32
    valid = hmac.compare_digest(password_digest(password, salt), expected)
    return row[0] if row and valid else None

def account_gate():
    if st.session_state.get("authenticated_user"):
        return
    st.markdown('<div class="eyebrow">SECURE WORKSPACE / ACCOUNT</div>', unsafe_allow_html=True)
    st.title("Welcome to InsightSQL Pro")
    st.caption("Sign in to access your private data workspace. Create an account once; use it again on future visits to this installation.")
    _, center, _ = st.columns([1, 1.3, 1])
    with center:
        sign_in, register = st.tabs(["Sign in", "Create account"])
        with sign_in:
            with st.form("login_form", clear_on_submit=False):
                login_name = st.text_input("Username", key="login_name", autocomplete="username")
                login_password = st.text_input("Password", type="password", key="login_password", autocomplete="current-password")
                login_clicked = st.form_submit_button("Sign in", type="primary", use_container_width=True)
            if login_clicked:
                attempts = st.session_state.get("login_attempts", 0)
                if attempts >= 5:
                    st.error("Too many failed attempts in this session. Restart the session and try again.")
                else:
                    found = verify_account(login_name, login_password)
                    if found:
                        st.session_state.authenticated_user = found
                        st.session_state.login_attempts = 0
                        st.rerun()
                    else:
                        st.session_state.login_attempts = attempts + 1
                        st.error("Incorrect username or password.")
        with register:
            with st.form("register_form", clear_on_submit=True):
                new_name = st.text_input("Choose a username", key="new_name", autocomplete="username")
                new_password = st.text_input("Create password (12+ characters)", type="password", key="new_password", autocomplete="new-password")
                confirm_password = st.text_input("Confirm password", type="password", key="confirm_password", autocomplete="new-password")
                register_clicked = st.form_submit_button("Create account", type="primary", use_container_width=True)
            if register_clicked:
                if new_password != confirm_password:
                    st.error("Passwords do not match.")
                else:
                    try:
                        st.session_state.authenticated_user = create_account(new_name, new_password)
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
    st.stop()

# ---------------------------------
# STREAMLIT PAGE CONFIGURATION
# ---------------------------------
st.set_page_config(page_title="InsightSQL Pro", page_icon="◈", layout="wide")

st.markdown("""<style>
/* Quiet, editorial interface inspired by polished developer tools. */
:root {color-scheme:dark;}
[data-testid="stAppViewContainer"] {background:#0b0c0f; color:#eceef2;}
[data-testid="stHeader"] {background:rgba(11,12,15,.94);}
[data-testid="stSidebar"] {background:#101115; border-right:1px solid #24262d;}
.block-container {max-width:1160px; padding-top:3rem; padding-bottom:5rem;}
html,body,[class*="css"],.stApp {font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;}
h1,h2,h3 {letter-spacing:-.045em; font-weight:620!important;}
h1 {font-size:2.6rem!important; line-height:1.12!important;}
h3 {font-size:1.22rem!important;}
p,[data-testid="stCaptionContainer"] {color:#9b9da8;}
[data-testid="stSidebar"] h3 {font-size:1.05rem!important;}
[data-testid="stMetric"] {background:#111318; border:1px solid #292b32; padding:17px 19px; border-radius:9px;}
[data-testid="stMetricValue"] {font-size:1.75rem;letter-spacing:-.045em;}
[data-testid="stMetricLabel"] {color:#858995;}
[data-testid="stForm"], [data-testid="stExpander"] {background:#111318; border:1px solid #292b32; border-radius:9px;}
[data-testid="stForm"] {padding:15px 18px;}
[data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea {background:#14161b!important; border-color:#333640!important; border-radius:7px!important;}
.stButton>button {border-radius:7px; font-weight:550; transition:background .14s ease,border-color .14s ease;}
.stButton>button:hover {border-color:#a6a8b3;}
.stButton>button[kind="primary"], [data-testid="stFormSubmitButton"] button[kind="primary"] {background:#f0f1f4!important; color:#121317!important; border:1px solid #f0f1f4!important;}
.stButton>button[kind="primary"]:hover, [data-testid="stFormSubmitButton"] button[kind="primary"]:hover {background:#cfd2da!important; border-color:#cfd2da!important;}
[data-testid="stTabs"] button {font-weight:500; font-size:.9rem;}
[data-testid="stTabs"] [data-baseweb="tab-highlight"] {background:#d7d9e1;}
[data-testid="stDataFrame"] {border:1px solid #292b32; border-radius:8px; overflow:hidden;}
[data-testid="stFileUploader"] {border-radius:8px;}
hr {border-color:#25272e!important;}
.eyebrow {font-size:.71rem; font-weight:650; letter-spacing:.16em; text-transform:uppercase; color:#9b9da8; margin-bottom:14px;}
.product-description {font-size:1.05rem; line-height:1.65; color:#a0a3ad; max-width:660px; margin:12px 0 22px;}
.product-mark {display:inline-flex; width:33px; height:33px; border:1px solid #4c4e59; border-radius:8px; align-items:center; justify-content:center; font-size:18px; margin-right:10px; vertical-align:middle; color:#eeeef2;}
.status-line {display:flex; flex-wrap:wrap; gap:18px; color:#8c909c; font-size:.8rem; padding:0 0 14px;}
.status-line span:before {content:""; display:inline-block; width:6px; height:6px; background:#7caa8b; border-radius:50%; margin-right:8px; vertical-align:middle;}
.section-kicker {font-size:.72rem; letter-spacing:.13em; color:#888b95; text-transform:uppercase; font-weight:650; margin:18px 0 4px;}

/* Restrained violet halo: interactive on hover/focus, not constantly glowing. */
.stButton > button, [data-testid="stFormSubmitButton"] button,
[data-testid="stMetric"], [data-testid="stExpander"],
[data-testid="stFileUploader"], [data-testid="stDataFrame"] {
  transition: border-color .22s ease, box-shadow .22s ease,
              transform .22s ease, background-color .22s ease;
}
.stButton > button:hover, [data-testid="stFormSubmitButton"] button:hover {
  border-color:#9b8cff!important;
  box-shadow:0 0 0 1px rgba(155,140,255,.35), 0 0 22px rgba(124,92,255,.24);
  transform:translateY(-1px);
}
.stButton > button:focus-visible, [data-testid="stFormSubmitButton"] button:focus-visible,
[data-testid="stTextInput"] input:focus, [data-testid="stTextArea"] textarea:focus {
  outline:none!important;
  border-color:#a395ff!important;
  box-shadow:0 0 0 2px rgba(155,140,255,.22), 0 0 17px rgba(124,92,255,.17)!important;
}
[data-testid="stMetric"]:hover, [data-testid="stExpander"]:hover,
[data-testid="stFileUploader"]:hover, [data-testid="stDataFrame"]:hover {
  border-color:#575078;
  box-shadow:0 0 18px rgba(124,92,255,.10);
}
[data-testid="stTabs"] button:hover {color:#c4baff!important;}
@media (prefers-reduced-motion:reduce) {
  .stButton > button, [data-testid="stFormSubmitButton"] button,
  [data-testid="stMetric"], [data-testid="stExpander"],
  [data-testid="stFileUploader"], [data-testid="stDataFrame"] {transition:none!important;transform:none!important;}
}
</style>""", unsafe_allow_html=True)

account_gate()

st.markdown('<div class="eyebrow">DATA WORKSPACE / 01</div>', unsafe_allow_html=True)
st.markdown('<h1><span class="product-mark">⌘</span>InsightSQL Pro</h1>', unsafe_allow_html=True)
st.markdown('<div class="product-description">From raw data to clear answers. Explore a dataset, write SQL with local AI, and inspect the results in one focused workspace.</div>', unsafe_allow_html=True)
st.markdown('<div class="status-line"><span>Local AI</span><span>Read-only SQL</span><span>Private workspace</span></div>', unsafe_allow_html=True)

# ---------------------------------
# DATABASE UPLOAD
# ---------------------------------
with st.sidebar:
    st.markdown("### InsightSQL Pro")
    st.caption(f"Signed in as **{st.session_state.authenticated_user}**")
    if st.button("Sign out", key="sign_out", use_container_width=True):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()
    st.caption("WORKSPACE / DATA")
    st.divider()
    st.subheader("Data source")
    uploaded_db = st.file_uploader(
        "Upload SQLite, CSV, or Excel",
        type=["db", "sqlite", "sqlite3", "csv", "xlsx"],
        help="Upload up to 25 MB. CSV imports are limited to 100,000 rows; Excel to 15 sheets.",
    )

# ---------------------------------
# REQUIRE DATABASE UPLOAD
# ---------------------------------
if uploaded_db is None:
    st.info("Choose a SQLite, CSV, or Excel file in the sidebar to begin.")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("#### 01 / Connect")
        st.caption("Bring your own SQLite database, CSV file, or Excel workbook.")
    with c2:
        st.markdown("#### 02 / Ask")
        st.caption("Ask a question in English. Local Qwen generates a SQL query.")
    with c3:
        st.markdown("#### 03 / Explore")
        st.caption("Inspect results, visualizations, execution plans, and optimization tips.")
    st.stop()

upload_bytes = uploaded_db.getvalue()
if len(upload_bytes) > MAX_UPLOAD_BYTES:
    st.error("The current upload limit is 25 MB.")
    st.stop()

# Cache conversion so chart interactions do not rebuild the database.
@st.cache_data(show_spinner=False, max_entries=8)
def prepare_database(file_name, file_bytes):
    ext = Path(file_name).suffix.lower()
    if ext in {".db", ".sqlite", ".sqlite3"}:
        if not file_bytes.startswith(b"SQLite format 3\x00"):
            raise ValueError("Invalid SQLite file header.")
        return file_bytes
    if ext == ".csv":
        frames = {"uploaded_data": pd.read_csv(io.BytesIO(file_bytes), nrows=MAX_IMPORT_ROWS + 1)}
    elif ext == ".xlsx":
        book = pd.ExcelFile(io.BytesIO(file_bytes), engine="openpyxl")
        if len(book.sheet_names) > 15:
            raise ValueError("Excel workbooks are limited to 15 sheets.")
        frames = {name: pd.read_excel(book, sheet_name=name, nrows=MAX_IMPORT_ROWS + 1) for name in book.sheet_names}
    else:
        raise ValueError("Unsupported file type.")
    if any(len(frame) > MAX_IMPORT_ROWS for frame in frames.values()):
        raise ValueError("Each sheet/file is limited to 100,000 rows.")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        path = tmp.name
    try:
        db = sqlite3.connect(path)
        try:
            used = set()
            for name, frame in frames.items():
                safe = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_") or "uploaded_data"
                if safe[0].isdigit(): safe = "data_" + safe
                base, i = safe, 2
                while safe.casefold() in used:
                    safe = f"{base}_{i}"
                    i += 1
                used.add(safe.casefold())
                if len(frame.columns) == 0: continue
                frame.columns = [str(c) for c in frame.columns]
                frame.to_sql(safe, db, if_exists="replace", index=False)
            db.commit()
        finally:
            db.close()
        return Path(path).read_bytes()
    finally:
        os.remove(path)

try:
    normalized_bytes = prepare_database(uploaded_db.name, upload_bytes)
except Exception as e:
    st.error(f"Could not import dataset: {e}")
    st.stop()

with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_file:
    temp_file.write(normalized_bytes)
    database_path = temp_file.name

try:
    # ---------------------------------
    # DISCOVER UPLOADED DATABASE TABLES
    # ---------------------------------
    # Table discovery and schema detection use the same uploaded database.
    db = open_read_only_connection(database_path)
    try:
        table_names = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
        database_schema = {
            name: db.execute("SELECT name, type FROM pragma_table_info(?)", (name,)).fetchall()
            for name in table_names
        }
    finally:
        db.close()
    if not table_names:
        st.warning("No tables were found in the uploaded dataset.")
        st.stop()

    with st.sidebar:
        st.success(f"✓ {uploaded_db.name}")
        st.caption(f"{len(upload_bytes)/1024:.1f} KB · {len(table_names)} table(s)")
        st.divider()
        st.subheader("Schema")
        for table_name, columns in database_schema.items():
            with st.expander(f"▦ {table_name} · {len(columns)} columns"):
                for column_name, data_type in columns:
                    st.caption(f"{column_name}  ·  {data_type or 'UNKNOWN'}")
        st.divider()
        st.caption("Qwen2.5-Coder · SQLite")

    # ---------------------------------
    # AUTOMATIC DASHBOARD
    # ---------------------------------
    st.subheader("Overview")
    overview = []
    db = open_read_only_connection(database_path)
    try:
        for name in table_names[:15]:
            quoted = '"' + name.replace('"', '""') + '"'
            try:
                row_count = db.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            except sqlite3.Error:
                row_count = None
            overview.append((name, row_count, len(database_schema[name])))
    finally:
        db.close()
    c1, c2, c3 = st.columns(3)
    c1.metric("Tables", len(table_names))
    c2.metric("Columns", sum(len(cols) for cols in database_schema.values()))
    c3.metric("Rows", f"{sum(r for _, r, _ in overview if isinstance(r, int)):,}")
    with st.expander("View dataset inventory", expanded=False):
        st.dataframe(pd.DataFrame(overview, columns=["Table", "Rows", "Columns"]), hide_index=True, use_container_width=True)

    # ---------------------------------
    # INTERACTIVE DATA EXPLORER
    # ---------------------------------
    st.subheader("Data explorer")
    explorer_tab, quality_tab = st.tabs(["Table preview", "Data quality"])
    with explorer_tab:
        selected_table = st.selectbox("Choose a table", table_names, key="explorer_table")
        quoted_table = '"' + selected_table.replace('"', '""') + '"'
        preview_limit = st.slider("Preview rows", 5, 100, 15, 5, key="preview_limit")
        db = open_read_only_connection(database_path)
        try:
            preview = pd.read_sql_query(f"SELECT * FROM {quoted_table} LIMIT {preview_limit}", db)
        finally:
            db.close()
        st.dataframe(preview, use_container_width=True, hide_index=True)
        st.download_button("Export preview", preview.to_csv(index=False).encode("utf-8"),
                           "table_preview.csv", "text/csv", key="preview_download")
    with quality_tab:
        st.caption("Quick quality profile of the previewed rows (not the entire database).")
        if not preview.empty:
            profile = pd.DataFrame({
                "Column": [str(c) for c in preview.columns],
                "Type": [str(preview[c].dtype) for c in preview.columns],
                "Missing": [int(preview[c].isna().sum()) for c in preview.columns],
                "Unique": [int(preview[c].nunique(dropna=True)) for c in preview.columns],
            })
            st.dataframe(profile, use_container_width=True, hide_index=True)
            st.progress(1 - min(1.0, float(preview.isna().sum().sum()) / max(1, preview.size)),
                        text="Completeness in preview")
        else:
            st.info("This table has no rows to profile.")

    # ---------------------------------
    # SIDEBAR
    # ---------------------------------
    # Sidebar controls are grouped above; no duplicate upload or explorer.

    # ---------------------------------
    # USER QUESTION INPUT
    # ---------------------------------
    st.divider()
    st.subheader("Ask your data")
    # Suggestions are based on actual uploaded columns, not a fixed sales dataset.
    first_table = table_names[0]
    first_columns = [col[0] for col in database_schema[first_table]]
    suggestions = [
        f"Show the first 20 rows from {first_table}",
        f"Count all rows in {first_table}",
    ]
    if first_columns:
        suggestions.append(f"Show distinct values of {first_columns[0]} in {first_table}")
    st.caption("Try an example · click to fill the question")
    chips = st.columns(len(suggestions))
    for i, (chip, suggestion) in enumerate(zip(chips, suggestions)):
        with chip:
            if st.button(suggestion, key=f"suggestion_{i}", use_container_width=True):
                st.session_state.question_text = suggestion
    with st.form("question_form"):
        user_question = st.text_input(
            "What would you like to know?",
            placeholder="e.g. Show the top 5 customers by total revenue",
            key="question_text",
        )
        generate_clicked = st.form_submit_button("Generate SQL  →", type="primary")

    schema_items = tuple((name, tuple(tuple(c) for c in columns)) for name, columns in database_schema.items())
    upload_id = hashlib.sha256(normalized_bytes).hexdigest()
    if st.session_state.get("active_upload_id") != upload_id:
        st.session_state.active_upload_id = upload_id
        st.session_state.question_text = ""
        st.session_state.generated_sql = None
        st.session_state.generated_question = None
        st.session_state.query_results = None
        st.session_state.query_plan = None
        st.session_state.query_time = None

    # ---------------------------------
    # GENERATE SQL
    # ---------------------------------
    if generate_clicked:
        st.session_state.query_results = None
        st.session_state.query_plan = None
        st.session_state.query_time = None
        if not user_question.strip():
            st.warning("Enter a question first.")
        else:
            with st.spinner("Qwen is writing your SQL..."):
                try:
                    candidate = generate_sql_ai(user_question, schema_items)
                    if candidate:
                        validate_sql(candidate, database_schema)
                    st.session_state.generated_sql = candidate
                    st.session_state.generated_question = user_question
                except Exception as e:
                    st.session_state.generated_sql = None
                    st.error(f"AI generation failed or SQL rejected: {e}")

    sql = st.session_state.get("generated_sql")
    chart_title = st.session_state.get("generated_question") or "Query results"

    # ---------------------------------
    # DISPLAY GENERATED SQL
    # ---------------------------------
    if sql:
        st.subheader("Generated SQL")
        st.code(sql, language="sql")
        st.caption("Review AI-generated SQL before running it. Only restricted read-only queries are permitted.")
        with st.expander("Edit SQL"):
            edited_sql = st.text_area("SQL editor", value=sql, height=130,
                                      key="sql_editor_" + hashlib.sha256(sql.encode()).hexdigest()[:12])
            if st.button("Apply edited SQL", key="apply_sql"):
                try:
                    clean = validate_sql(edited_sql, database_schema)
                    st.session_state.generated_sql = clean
                    st.session_state.query_results = None
                    st.session_state.query_plan = None
                    st.rerun()
                except Exception as e:
                    st.error(f"SQL rejected: {e}")

        # ---------------------------------
        # VALIDATE AND EXECUTE
        # ---------------------------------
        if st.button("Run query  →", type="primary"):
            try:
                started = time.perf_counter()
                df, plan = run_query(sql, database_path, database_schema)
                st.session_state.query_time = (time.perf_counter() - started) * 1000
                st.session_state.query_results = df
                st.session_state.query_plan = plan
            except Exception as e:
                st.session_state.query_results = None
                st.error(f"Query rejected or failed: {e}")

        df = st.session_state.get("query_results")
        if df is not None:
            st.caption("Query completed successfully")
            m1, m2 = st.columns(2)
            m1.metric("Execution time", f"{st.session_state.query_time:.2f} ms")
            m2.metric("Rows returned", len(df))

            # RESULTS TABLE + DATA VISUALIZATION + SQL EXECUTION PLAN
            result_tab, visual_tab, explain_tab = st.tabs([
                "Results", "Visualize", "Query insights"
            ])
            with result_tab:
                search_text = st.text_input("Filter visible results", placeholder="Search across columns…", key="result_search")
                visible = df
                if search_text.strip():
                    matches = df.astype(str).apply(
                        lambda col: col.str.contains(search_text, case=False, regex=False, na=False)
                    ).any(axis=1)
                    visible = df.loc[matches]
                st.caption(f"Showing {len(visible)} of {len(df)} returned rows")
                st.dataframe(visible, use_container_width=True, hide_index=True)
                st.download_button("Download CSV", visible.to_csv(index=False).encode("utf-8"),
                                   "insightsql_results.csv", "text/csv")
            with visual_tab:
                numeric = list(df.select_dtypes(include="number").columns)
                if df.empty or not numeric:
                    st.info("Run a query with numeric results to unlock charts.")
                else:
                    a, b, c = st.columns(3)
                    x_choice = a.selectbox("X axis", list(df.columns), key="chart_x")
                    y_choice = b.selectbox("Y axis", numeric, key="chart_y")
                    chart_kind = c.selectbox("Chart type", ["Bar", "Line", "Scatter", "Pie", "Histogram"], key="chart_kind")
                    limit = st.slider("Maximum plotted rows", 1, 200, min(50, len(df)), key="plot_limit")
                    chart_df = df.head(limit)
                    try:
                        if chart_kind == "Bar": fig = px.bar(chart_df, x=x_choice, y=y_choice, title=chart_title)
                        elif chart_kind == "Line": fig = px.line(chart_df, x=x_choice, y=y_choice, markers=True, title=chart_title)
                        elif chart_kind == "Scatter": fig = px.scatter(chart_df, x=x_choice, y=y_choice, title=chart_title)
                        elif chart_kind == "Pie": fig = px.pie(chart_df, names=x_choice, values=y_choice, title=chart_title)
                        else: fig = px.histogram(chart_df, x=y_choice, title=chart_title)
                        fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                                          plot_bgcolor="rgba(0,0,0,0)", font=dict(color="#c9cbd3"),
                                          colorway=["#aeb7cf", "#789b9a", "#c0a58c", "#9d91ad"],
                                          margin=dict(l=15, r=15, t=50, b=15))
                        st.plotly_chart(fig, use_container_width=True)
                    except Exception as e:
                        st.warning(f"Chart unavailable: {e}")
            with explain_tab:
                st.markdown("**SQLite execution plan**")
                st.dataframe(pd.DataFrame(st.session_state.query_plan,
                                          columns=["id", "parent", "unused", "detail"]),
                             hide_index=True, use_container_width=True)
                st.markdown("**Optimization ideas**")
                for tip in analyze_sql(sql):
                    st.write("• " + tip)
    elif st.session_state.get("generated_question"):
        st.warning("That question could not be answered using the uploaded schema.")
finally:
    try:
        os.remove(database_path)
    except OSError:
        pass
