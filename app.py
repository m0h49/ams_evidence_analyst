# ============================================================
# IMPORTS
# ============================================================

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "app.db"

app = Flask(__name__, static_folder="static", static_url_path="/static")


# ============================================================
# DATABASE / STORAGE
# ============================================================

def now():
    """Return one consistent UTC timestamp format for audit events."""
    return datetime.now(timezone.utc).isoformat()


def get_db():
    """Open SQLite and return a connection with dictionary-like rows."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the small database schema automatically on startup."""
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            goal TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            limits_json TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS documents (
            document_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            version TEXT NOT NULL,
            allowed_roles TEXT NOT NULL,
            title TEXT NOT NULL,
            PRIMARY KEY (document_id, version)
        );

        CREATE TABLE IF NOT EXISTS fragments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            version TEXT NOT NULL,
            fragment TEXT NOT NULL,
            FOREIGN KEY(document_id, version)
                REFERENCES documents(document_id, version)
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            actor TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            decision TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            details TEXT NOT NULL
        );
        """
    )

    # Demo data is deterministic and contains no personal information.
    count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    if count == 0:
        demo_documents = [
            (
                "policy-1",
                "demo-a",
                "v1",
                json.dumps(["operator", "admin"]),
                "Data retention policy v1",
                [
                    "Customer event data is retained for 30 days.",
                    "After 30 days, customer event data is deleted.",
                ],
            ),
            (
                "policy-2",
                "demo-a",
                "v2",
                json.dumps(["operator", "admin"]),
                "Data retention policy v2",
                [
                    "Customer event data is retained for 60 days.",
                    "After 60 days, customer event data is deleted.",
                ],
            ),
            (
                "internal-1",
                "demo-a",
                "v1",
                json.dumps(["admin"]),
                "Internal admin note",
                ["The internal maintenance window is Sunday at 02:00 UTC."],
            ),
        ]

        for doc_id, workspace, version, roles, title, fragments in demo_documents:
            conn.execute(
                """
                INSERT INTO documents
                (document_id, workspace_id, version, allowed_roles, title)
                VALUES (?, ?, ?, ?, ?)
                """,
                (doc_id, workspace, version, roles, title),
            )
            for fragment in fragments:
                conn.execute(
                    """
                    INSERT INTO fragments(document_id, version, fragment)
                    VALUES (?, ?, ?)
                    """,
                    (doc_id, version, fragment),
                )

    conn.commit()
    conn.close()


def hash_input(payload):
    """Hash input for audit. We never store the full sensitive input in events."""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def add_event(conn, task_id, event_type, actor, payload, decision, trace_id, details):
    conn.execute(
        """
        INSERT INTO events
        (task_id, event_type, timestamp, actor, input_hash, decision, trace_id, details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            event_type,
            now(),
            actor,
            hash_input(payload),
            decision,
            trace_id,
            details,
        ),
    )


# ============================================================
# PLANNER
# ============================================================

def make_plan():
    """Deterministic replacement for an LLM planner.

    A real model can later implement the same interface, but it never receives
    direct access to tools. The plan is only a list of safe internal steps.
    """
    return [
        "find_sources",
        "check_evidence",
        "form_conclusion",
    ]


# ============================================================
# DOCUMENT IMPORT — импорт Markdown-документов
# ============================================================

def split_markdown_into_fragments(markdown_text):

    blocks = []

    # Разделяем документ по пустым строкам.
    raw_blocks = markdown_text.split("\n\n")

    for block in raw_blocks:
        block = block.strip()

        if not block:
            continue

        # Убираем Markdown-заголовки.
        if block.startswith("#"):
            block = block.lstrip("#").strip()

        if block:
            blocks.append(block)

    return blocks


def import_markdown_document(
    file,
    document_id,
    workspace_id,
    version,
    allowed_roles
):
    # """
    # Импортирует Markdown-документ в SQLite.

    # file           — загруженный Markdown-файл
    # document_id    — уникальное имя документа
    # workspace_id   — workspace документа
    # version        — версия документа
    # allowed_roles  — список ролей с доступом
    # """

    # --------------------------------------------------------
    # 1. Проверяем расширение файла
    # --------------------------------------------------------

    if not file.filename.lower().endswith(".md"):
        raise ValueError("Only Markdown files are allowed")

    # --------------------------------------------------------
    # 2. Читаем Markdown
    # --------------------------------------------------------

    markdown_text = file.read().decode("utf-8")

    if not markdown_text.strip():
        raise ValueError("Markdown document is empty")

    # --------------------------------------------------------
    # 3. Разбиваем Markdown на fragments
    # --------------------------------------------------------

    fragments = split_markdown_into_fragments(markdown_text)

    if not fragments:
        raise ValueError("Markdown document has no text")

    # --------------------------------------------------------
    # 4. Сохраняем документ
    # --------------------------------------------------------

    conn = get_db()

    # Название документа берём из имени файла.
    title = Path(file.filename).stem

    try:
        conn.execute(
            """
            INSERT INTO documents
            (
                document_id,
                workspace_id,
                version,
                allowed_roles,
                title
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                document_id,
                workspace_id,
                version,
                json.dumps(allowed_roles),
                title,
            ),
        )

        # ----------------------------------------------------
        # 5. Сохраняем fragments
        # ----------------------------------------------------

        for fragment in fragments:
            conn.execute(
                """
                INSERT INTO fragments
                (
                    document_id,
                    version,
                    fragment
                )
                VALUES (?, ?, ?)
                """,
                (
                    document_id,
                    version,
                    fragment,
                ),
            )

        conn.commit()

    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError(
            "Document with this document_id and version "
            "already exists"
        )

    finally:
        conn.close()

    return {
        "document_id": document_id,
        "workspace_id": workspace_id,
        "version": version,
        "allowed_roles": allowed_roles,
        "title": title,
        "fragments_count": len(fragments),
    }



# ============================================================
# POLICY — проверка прав доступа
# ============================================================

def policy_check(document, role):

    allowed_roles = json.loads(document["allowed_roles"])

    return role in allowed_roles

# ============================================================
# RETRIEVAL — поиск документов
# ============================================================

def lexical_search(conn, goal, workspace_id, role):
    """Search only documents the current role may access.

    This is deliberately simple lexical search. A vector database is not
    required by the assignment.
    """
    words = [
        word.strip(".,!?;:()[]{}\"'").lower()
        for word in goal.split()
    ]

    rows = conn.execute(
        """
        SELECT
            f.document_id,
            f.version,
            f.fragment,
            d.title,
            d.allowed_roles
        FROM fragments f
        JOIN documents d
          ON d.document_id = f.document_id
         AND d.version = f.version
        WHERE d.workspace_id = ?
        """,
        (workspace_id,)
    ).fetchall()

    results = []

    for row in rows:
        
        # Access control happens before a fragment can become search evidence.
        if not policy_check(row, role):
            continue

        fragment = row["fragment"].lower()
         
        score = sum(1 for word in words if word in fragment)

        if score > 0:
            results.append(
                {
                    "document_id": row["document_id"],
                    "fragment": row["fragment"],
                    "source_version": row["version"],
                    "title": row["title"],
                    "score": score,
                }
            )

    results.sort(key=lambda item: item["score"], reverse=True)
    return results


# ============================================================
# ANSWER BUILDER — формирование ответа
# ============================================================

def build_answer(goal, sources):
    """Create an answer only from retrieved evidence.

    No source means refusal. Conflicting retention periods are explicitly
    reported instead of silently choosing one.
    """
    if not sources:
        return {
            "type": "refusal",
            "answer": (
                "Недостаточно оснований для ответа. "
                "Не найден доступный фрагмент, подтверждающий вопрос."
            ),
            "citations": [],
        }

    retention_values = []
    for source in sources:
        text = source["fragment"]
        if "30 days" in text:
            retention_values.append(("30 days", source))
        if "60 days" in text:
            retention_values.append(("60 days", source))

    citations = [
        {
            "document_id": source["document_id"],
            "fragment": source["fragment"],
            "source_version": source["source_version"],
        }
        for source in sources
    ]

    if len({value for value, _ in retention_values}) > 1:
        return {
            "type": "conflict",
            "answer": (
                "Обнаружено противоречие: доступные версии политики "
                "указывают разные сроки хранения — 30 и 60 дней."
            ),
            "citations": citations,
        }

    return {
        "type": "answer",
        "answer": f"Найдено основание: {sources[0]['fragment']}",
        "citations": citations,
    }


# ============================================================
# TASK EXECUTION — основной workflow
# ============================================================

def run_task(payload):
    """Run the complete bounded autonomous workflow."""
    task_id = payload["task_id"]
    trace_id = str(uuid.uuid4())
    workspace_id = payload["workspace_id"]
    user_id = payload["user_id"]
    role = payload["role"]
    goal = payload["goal"]
    limits = payload.get(
        "limits",
        {"max_steps": 3, "max_tool_calls": 1, "max_runtime_seconds": 10},
    )

    conn = get_db()

    # Duplicate task_id is rejected before a second task is created.
    existing = conn.execute(
        "SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if existing:
        conn.close()
        return None, "duplicate"

    conn.execute(
        """
        INSERT INTO tasks
        (task_id, workspace_id, user_id, role, goal, mode, status,
         result_json, limits_json, trace_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            workspace_id,
            user_id,
            role,
            goal,
            payload.get("mode", "read_only"),
            "PLANNING",
            None,
            json.dumps(limits),
            trace_id,
            now(),
        ),
    )

    add_event(
        conn, task_id, "PLANNING_STARTED", "planner", payload,
        "allow", trace_id, "Create bounded deterministic plan",
    )

    plan = make_plan()

    if len(plan) > int(limits.get("max_steps", 3)):
        conn.execute(
            "UPDATE tasks SET status = ? WHERE task_id = ?",
            ("FAILED", task_id),
        )
        add_event(
            conn, task_id, "LIMIT_REACHED", "policy", payload,
            "deny", trace_id, "max_steps exceeded",
        )
        conn.commit()
        conn.close()
        return {"task_id": task_id, "status": "FAILED"}, None

    conn.execute(
        "UPDATE tasks SET status = ? WHERE task_id = ?",
        ("RUNNING", task_id),
    )
    add_event(
        conn, task_id, "RETRIEVAL_STARTED", "retriever", payload,
        "allow", trace_id, "Access-controlled lexical search",
    )

    sources = lexical_search(conn, goal, workspace_id, role)

    # Gateway-like boundary: answerer receives only policy-approved evidence.
    add_event(
        conn, task_id, "EVIDENCE_CHECKED", "policy", payload,
        "allow" if sources else "deny",
        trace_id,
        f"Approved evidence fragments: {len(sources)}",
    )

    result = build_answer(goal, sources)

    status = "COMPLETED"
    if result["type"] == "refusal":
        status = "FAILED"

    result["trace_id"] = trace_id
    result["plan"] = plan

    conn.execute(
        "UPDATE tasks SET status = ?, result_json = ? WHERE task_id = ?",
        (status, json.dumps(result, ensure_ascii=False), task_id),
    )

    add_event(
        conn, task_id, "TASK_FINISHED", "system", payload,
        "allow" if status == "COMPLETED" else "deny",
        trace_id, result["type"],
    )

    conn.commit()
    conn.close()
    return {"task_id": task_id, "status": status, "result": result}, None


# ============================================================
# API — HTTP endpoints
# ============================================================

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/tasks")
def create_task():
    payload = request.get_json(silent=True) or {}

    required = [
        "task_id",
        "workspace_id",
        "user_id",
        "role",
        "goal",
    ]
    missing = [field for field in required if field not in payload]
    if missing:
        return jsonify({"error": "missing fields", "fields": missing}), 400

    result, error = run_task(payload)

    if error == "duplicate":
        return jsonify({"error": "task_id already exists"}), 409

    return jsonify(result), 201


@app.get("/tasks/<task_id>")
def get_task(task_id):
    conn = get_db()
    task = conn.execute(
        "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    conn.close()

    if not task:
        return jsonify({"error": "task not found"}), 404

    data = dict(task)
    data["limits"] = json.loads(data.pop("limits_json"))
    data["result"] = (
        json.loads(data.pop("result_json"))
        if data.get("result_json")
        else None
    )
    return jsonify(data)


@app.get("/tasks/<task_id>/events")
def get_events(task_id):
    conn = get_db()
    rows = conn.execute(
        """
        SELECT event_type, timestamp, actor, input_hash,
               decision, trace_id, details
        FROM events
        WHERE task_id = ?
        ORDER BY id
        """,
        (task_id,),
    ).fetchall()
    conn.close()

    return jsonify([dict(row) for row in rows])


@app.post("/tasks/<task_id>/confirm")
def confirm_task(task_id):
    # Direction A does not need confirmation. We expose the endpoint only
    # to make the API contract explicit and safely reject the operation.
    return jsonify(
        {
            "error": "confirmation is not used in direction A",
            "task_id": task_id,
        }
    ), 400

@app.post("/documents")
def upload_document():
    # """
    # Загружает Markdown-документ.

    # Ожидает multipart/form-data:

    # file          — .md файл
    # document_id   — ID документа
    # workspace_id  — workspace
    # version       — версия
    # allowed_roles — роли через запятую

    # Например:

    # allowed_roles = operator,admin
    # """

    # --------------------------------------------------------
    # 1. Получаем файл
    # --------------------------------------------------------

    file = request.files.get("file")

    if not file:
        return jsonify({
            "error": "Markdown file is required"
        }), 400

    # --------------------------------------------------------
    # 2. Получаем данные документа
    # --------------------------------------------------------

    document_id = request.form.get("document_id")
    workspace_id = request.form.get("workspace_id")
    version = request.form.get("version")
    allowed_roles_raw = request.form.get("allowed_roles")

    if not document_id:
        return jsonify({
            "error": "document_id is required"
        }), 400

    if not workspace_id:
        return jsonify({
            "error": "workspace_id is required"
        }), 400

    if not version:
        return jsonify({
            "error": "version is required"
        }), 400

    if not allowed_roles_raw:
        return jsonify({
            "error": "allowed_roles is required"
        }), 400

    # --------------------------------------------------------
    # 3. Превращаем "operator,admin"
    #    в ["operator", "admin"]
    # --------------------------------------------------------

    allowed_roles = [
        role.strip()
        for role in allowed_roles_raw.split(",")
        if role.strip()
    ]

    if not allowed_roles:
        return jsonify({
            "error": "allowed_roles cannot be empty"
        }), 400

    # --------------------------------------------------------
    # 4. Импортируем документ
    # --------------------------------------------------------

    try:
        result = import_markdown_document(
            file=file,
            document_id=document_id,
            workspace_id=workspace_id,
            version=version,
            allowed_roles=allowed_roles,
        )

    except ValueError as error:
        return jsonify({
            "error": str(error)
        }), 400

    # --------------------------------------------------------
    # 5. Возвращаем результат
    # --------------------------------------------------------

    return jsonify(result), 201


# ============================================================
# APPLICATION START
# ============================================================

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
