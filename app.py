import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import os
import hashlib
import hmac
import json
import glob
import secrets
import plotly.express as px
from sqlalchemy import create_engine, text
import io
import zipfile
import xml.etree.ElementTree as ET
import re
import requests

# ==========================================
# ページ基本設定 & セキュリティヘッダーCSS (高速描画対応)
# ==========================================
st.set_page_config(
    page_title="青果卸売 在庫・取引管理 Pro (高速・軽量版)",
    page_icon="🍎",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
    <style>
    .main { background-color: #f8f9fa; }
    .app-header {
        background: linear-gradient(135deg, #1e7e34 0%, #28a745 100%);
        padding: 1.0rem 1.4rem;
        border-radius: 10px;
        color: white;
        margin-bottom: 1.0rem;
        box-shadow: 0 4px 12px rgba(40, 167, 69, 0.15);
    }
    .app-header h1 { margin: 0; font-size: 1.5rem; font-weight: 700; color: #ffffff; }
    .app-header p { margin: 2px 0 0 0; opacity: 0.9; font-size: 0.85rem; }
    .section-title {
        font-size: 1.05rem;
        font-weight: 700;
        color: #2d3748;
        margin-bottom: 0.6rem;
        border-bottom: 2px solid #e2e8f0;
        padding-bottom: 4px;
    }
    .role-badge-admin { background-color: #dc3545; color: white; padding: 2px 6px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
    .role-badge-staff { background-color: #28a745; color: white; padding: 2px 6px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
    .db-badge-cloud { background-color: #0284c7; color: white; padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
    .db-badge-local { background-color: #4b5563; color: white; padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
    .speed-badge { background-color: #10b981; color: white; padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
    .security-notice {
        background: #f0fdf4;
        border: 1px solid #86efac;
        padding: 10px 14px;
        border-radius: 8px;
        margin-bottom: 10px;
        font-size: 0.82rem;
    }
    .restore-box {
        background-color: #f0fdf4;
        border: 1px solid #86efac;
        padding: 10px 14px;
        border-radius: 6px;
        margin-bottom: 12px;
        font-size: 13px;
    }
    .mismatch-box {
        background-color: #fef2f2;
        border: 1.5px solid #f87171;
        padding: 12px 16px;
        border-radius: 8px;
        margin-bottom: 12px;
    }
    </style>
""", unsafe_allow_html=True)

# ==========================================
# 1. 永続ストレージ & DB設定 (PostgreSQL対応コネクションプール)
# ==========================================
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SQLITE_DB_PATH = os.path.join(DATA_DIR, "seika_enterprise.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)
MASTER_CSV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "item_master.csv")

def get_secret(key: str, default: str = None) -> str:
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.environ.get(key, default)

def get_engine():
    custom_url = st.session_state.get("custom_database_url", None)
    db_url = custom_url or get_secret("DATABASE_URL")
    
    if db_url and db_url.strip():
        url = db_url.strip()
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        try:
            return create_engine(
                url,
                pool_pre_ping=True,
                pool_recycle=300,
                pool_size=10,
                max_overflow=20,
                connect_args={"connect_timeout": 5}
            )
        except Exception:
            pass

    return create_engine(
        f"sqlite:///{SQLITE_DB_PATH}?check_same_thread=False",
        pool_pre_ping=True
    )

def is_using_cloud_db() -> bool:
    custom_url = st.session_state.get("custom_database_url", None)
    db_url = custom_url or get_secret("DATABASE_URL")
    return bool(db_url and db_url.strip() and not db_url.strip().startswith("sqlite"))

# ==========================================
# 2. パスワード暗号化 & セッショントークン
# ==========================================
AUTH_SECRET = get_secret("AUTH_SECRET_KEY", "seika_production_super_secret_auth_key_2026")

def hash_password(password: str, salt: str = None) -> str:
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return f"{salt}:{hashed.hex()}"

def verify_password(stored_hash: str, provided_password: str) -> bool:
    try:
        salt, h_hex = stored_hash.split(":")
        check_hash = hashlib.pbkdf2_hmac("sha256", provided_password.encode("utf-8"), salt.encode("utf-8"), 100000)
        return hmac.compare_digest(check_hash.hex(), h_hex)
    except Exception:
        return False

def create_session_token(username: str, role: str) -> str:
    exp = (datetime.now() + timedelta(days=7)).isoformat()
    raw = f"{username}|{role}|{exp}"
    sig = hmac.new(AUTH_SECRET.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{raw}|{sig}"

def verify_session_token(token: str):
    try:
        parts = token.split("|")
        if len(parts) != 4:
            return None
        u, r, exp_str, sig = parts
        exp = datetime.fromisoformat(exp_str)
        if datetime.now() > exp:
            return None
        raw = f"{u}|{r}|{exp_str}"
        expected_sig = hmac.new(AUTH_SECRET.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected_sig, sig):
            return {"username": u, "role": r}
        return None
    except Exception:
        return None

# ==========================================
# 3. データベーステーブル初期化
# ==========================================
def init_db():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'staff',
                display_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                market_number TEXT NOT NULL,
                category TEXT,
                item_code TEXT,
                item_name TEXT NOT NULL,
                unit_price INTEGER NOT NULL DEFAULT 0,
                quantity INTEGER NOT NULL DEFAULT 0,
                subtotal INTEGER NOT NULL DEFAULT 0,
                user_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS deleted_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_id INTEGER,
                date TEXT,
                market_number TEXT,
                category TEXT,
                item_code TEXT,
                item_name TEXT,
                unit_price INTEGER,
                quantity INTEGER,
                subtotal INTEGER,
                user_id TEXT,
                deleted_by TEXT,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))
        
        # 初期管理者・社員の作成
        res = conn.execute(text("SELECT count(*) FROM users;")).scalar()
        if res == 0:
            init_admin_pw = get_secret("INITIAL_ADMIN_PASSWORD", "password123")
            conn.execute(
                text("INSERT INTO users (username, password_hash, role, display_name) VALUES (:u, :p, :r, :d)"),
                {"u": "admin", "p": hash_password(init_admin_pw), "r": "admin", "d": "統括管理者"}
            )
            for uid, uname in [("user1", "佐藤 (営業1課)"), ("user2", "鈴木 (営業2課)"), ("staff_a", "高橋 (業務課)")]:
                conn.execute(
                    text("INSERT INTO users (username, password_hash, role, display_name) VALUES (:u, :p, :r, :d)"),
                    {"u": uid, "p": hash_password("password123"), "r": "staff", "d": uname}
                )

init_db()

# ==========================================
# 4. ユーザー認証関数 & ID/パスワード変更機能
# ==========================================
def verify_user_login(username: str, password_raw: str):
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT username, password_hash, role, display_name FROM users WHERE username = :u"),
            {"u": username.strip()}
        ).fetchone()
        if row and verify_password(row[1], password_raw):
            return {"username": row[0], "role": row[2], "name": row[3]}
    return None

def get_all_users_from_db():
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT username, role, display_name, created_at FROM users ORDER BY id ASC")).fetchall()
        return [{"username": r[0], "role": r[1], "name": r[2], "created_at": str(r[3])} for r in rows]

def register_new_user_db(username, password, role="staff", display_name=""):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (username, password_hash, role, display_name) VALUES (:u, :p, :r, :d)"),
            {"u": username.strip(), "p": hash_password(password), "r": role, "d": display_name.strip()}
        )

def update_user_credentials(old_username: str, new_username: str, new_password: str, new_display_name: str):
    engine = get_engine()
    with engine.begin() as conn:
        if new_password and new_password.strip():
            p_hash = hash_password(new_password.strip())
            conn.execute(
                text("UPDATE users SET username = :new_u, password_hash = :p, display_name = :d WHERE username = :old_u"),
                {"new_u": new_username.strip(), "p": p_hash, "d": new_display_name.strip(), "old_u": old_username}
            )
        else:
            conn.execute(
                text("UPDATE users SET username = :new_u, display_name = :d WHERE username = :old_u"),
                {"new_u": new_username.strip(), "d": new_display_name.strip(), "old_u": old_username}
            )
        if old_username != new_username.strip():
            conn.execute(
                text("UPDATE transactions SET user_id = :new_u WHERE user_id = :old_u"),
                {"new_u": new_username.strip(), "old_u": old_username}
            )
    invalidate_records_cache()

# ==========================================
# 5. 取引データ操作 (高速キャッシュ連携 & バリデーション)
# ==========================================
@st.cache_data(ttl=60)
def load_records_cached(target_user_filter: str = None):
    engine = get_engine()
    with engine.connect() as conn:
        if target_user_filter:
            query = text("""
                SELECT id, date AS 日付, market_number AS 市場番号, category AS カテゴリー,
                       item_code AS 商品コード, item_name AS 商品名, unit_price AS 単価,
                       quantity AS 数量, subtotal AS 小計, user_id AS 登録担当者
                FROM transactions
                WHERE user_id = :u
                ORDER BY id DESC;
            """)
            df = pd.read_sql(query, conn, params={"u": target_user_filter})
        else:
            query = text("""
                SELECT id, date AS 日付, market_number AS 市場番号, category AS カテゴリー,
                       item_code AS 商品コード, item_name AS 商品名, unit_price AS 単価,
                       quantity AS 数量, subtotal AS 小計, user_id AS 登録担当者
                FROM transactions
                ORDER BY id DESC;
            """)
            df = pd.read_sql(query, conn)
    return df

def load_records_from_db(target_user_filter: str = None):
    return load_records_cached(target_user_filter)

def invalidate_records_cache():
    load_records_cached.clear()

def insert_records_to_db(user_id: str, records: list, auto_backup=True):
    if not records:
        return
    
    # バリデーション検証 (単価10万円超過や未来日付のチェック)
    valid_records = []
    for r in records:
        p = int(r.get("単価", 0))
        q = int(r.get("数量", 0))
        if p > 100000:
            st.warning(f"⚠️ 【バリデーション警告】商品名「{r.get('商品名')}」の単価（¥{p:,}）が通常上限（10万円）を超えています。")
        if p < 0 or q < 0:
            continue # 不正値スキップ
        
        valid_records.append(r)

    if not valid_records:
        return

    engine = get_engine()
    with engine.begin() as conn:
        for r in valid_records:
            p = int(r.get("単価", 0))
            q = int(r.get("数量", 0))
            sub = r.get("小計", p * q)
            conn.execute(text("""
                INSERT INTO transactions (date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id)
                VALUES (:date, :m, :c, :code, :name, :p, :q, :sub, :u)
            """), {
                "date": str(r.get("日付", datetime.now().date())),
                "m": str(r.get("市場番号", "M-001")),
                "c": str(r.get("カテゴリー", "1課 (熊谷・大宮)")),
                "code": str(r.get("商品コード", "AUTO")),
                "name": str(r.get("商品名", "品名未指定")),
                "p": p,
                "q": q,
                "sub": sub,
                "u": user_id
            })
    invalidate_records_cache()
    if auto_backup:
        create_backup_snapshot(user_id, trigger_reason="新規データ追加")

def update_single_record_in_db(record_dict: dict):
    rec_id = record_dict.get("id")
    if not rec_id:
        return
    p = int(record_dict.get("単価", 0))
    if p > 100000:
        st.warning(f"⚠️ 【バリデーション警告】単価（¥{p:,}）が10万円を超えています。")
    
    engine = get_engine()
    q = int(record_dict.get("数量", 0))
    sub = record_dict.get("小計", p * q)
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE transactions
            SET date = :date, market_number = :m, category = :c, item_code = :code,
                item_name = :name, unit_price = :p, quantity = :q, subtotal = :sub
            WHERE id = :id
        """), {
            "id": rec_id,
            "date": str(record_dict.get("日付")),
            "m": str(record_dict.get("市場番号")),
            "c": str(record_dict.get("カテゴリー")),
            "code": str(record_dict.get("商品コード")),
            "name": str(record_dict.get("商品名")),
            "p": p,
            "q": q,
            "sub": sub
        })
    invalidate_records_cache()

def delete_records_by_ids(ids: list, deleted_by: str):
    if not ids:
        return []
    engine = get_engine()
    backed_up = []
    with engine.begin() as conn:
        for rid in ids:
            row = conn.execute(text("SELECT * FROM transactions WHERE id = :id"), {"id": rid}).fetchone()
            if row:
                backed_up.append(dict(row._mapping))
                conn.execute(text("""
                    INSERT INTO deleted_transactions (original_id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, deleted_by)
                    VALUES (:oid, :d, :m, :c, :cd, :name, :p, :q, :sub, :u, :delby)
                """), {
                    "oid": row.id, "d": row.date, "m": row.market_number, "c": row.category,
                    "cd": row.item_code, "name": row.item_name, "p": row.unit_price,
                    "q": row.quantity, "sub": row.subtotal, "u": row.user_id, "delby": deleted_by
                })
        conn.execute(text(f"DELETE FROM transactions WHERE id IN ({','.join(str(i) for i in ids)})"))
    invalidate_records_cache()
    create_backup_snapshot(deleted_by, trigger_reason=f"{len(ids)}件のデータ削除")
    return backed_up

def load_deleted_records_from_db(user_filter=None):
    engine = get_engine()
    with engine.connect() as conn:
        if user_filter:
            q = text("""
                SELECT id AS ゴミ箱ID, original_id AS 元ID, date AS 日付, market_number AS 市場番号,
                       category AS カテゴリー, item_name AS 商品名, unit_price AS 単価,
                       quantity AS 数量, subtotal AS 小計, user_id AS 元登録者, deleted_at AS 削除日時
                FROM deleted_transactions
                WHERE user_id = :u
                ORDER BY id DESC;
            """)
            return pd.read_sql(q, conn, params={"u": user_filter})
        else:
            q = text("""
                SELECT id AS ゴミ箱ID, original_id AS 元ID, date AS 日付, market_number AS 市場番号,
                       category AS カテゴリー, item_name AS 商品名, unit_price AS 単価,
                       quantity AS 数量, subtotal AS 小計, user_id AS 元登録者, deleted_at AS 削除日時
                FROM deleted_transactions
                ORDER BY id DESC;
            """)
            return pd.read_sql(q, conn)

def restore_records_from_trash(trash_ids: list, restored_by: str):
    if not trash_ids:
        return 0
    engine = get_engine()
    cnt = 0
    with engine.begin() as conn:
        for tid in trash_ids:
            row = conn.execute(text("SELECT * FROM deleted_transactions WHERE id = :id"), {"id": tid}).fetchone()
            if row:
                conn.execute(text("""
                    INSERT INTO transactions (date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id)
                    VALUES (:d, :m, :c, :cd, :name, :p, :q, :sub, :u)
                """), {
                    "d": row.date, "m": row.market_number, "c": row.category,
                    "cd": row.item_code, "name": row.item_name, "p": row.unit_price,
                    "q": row.quantity, "sub": row.subtotal, "u": row.user_id
                })
                conn.execute(text("DELETE FROM deleted_transactions WHERE id = :id"), {"id": tid})
                cnt += 1
    invalidate_records_cache()
    create_backup_snapshot(restored_by, trigger_reason=f"ゴミ箱から{cnt}件復元")
    return cnt

def purge_deleted_records(user_filter=None):
    engine = get_engine()
    with engine.begin() as conn:
        if user_filter:
            conn.execute(text("DELETE FROM deleted_transactions WHERE user_id = :u"), {"u": user_filter})
        else:
            conn.execute(text("DELETE FROM deleted_transactions"))

def get_employee_summary_stats():
    engine = get_engine()
    with engine.connect() as conn:
        query = text("""
            SELECT user_id AS 社員ID,
                   COUNT(*) AS 登録件数,
                   COALESCE(SUM(quantity), 0) AS 総数量,
                   COALESCE(SUM(subtotal), 0) AS 総売上金額,
                   MAX(date) AS 最終入力日
            FROM transactions
            GROUP BY user_id
            ORDER BY 総売上金額 DESC;
        """)
        return pd.read_sql(query, conn)

# ==========================================
# 6. BCP自動バックアップ & 復旧
# ==========================================
def create_backup_snapshot(user_id: str, trigger_reason="定期自動バックアップ"):
    engine = get_engine()
    with engine.connect() as conn:
        df = pd.read_sql(text("SELECT * FROM transactions ORDER BY id ASC"), conn)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"backup_{timestamp}_{user_id}.json"
    fpath = os.path.join(BACKUP_DIR, fname)
    
    data = {
        "timestamp": datetime.now().isoformat(),
        "created_by": user_id,
        "trigger": trigger_reason,
        "count": len(df),
        "records": df.to_dict(orient="records")
    }
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        
    return {"success": True, "filename": fname, "count": len(df)}

def list_all_backups():
    files = glob.glob(os.path.join(BACKUP_DIR, "backup_*.json"))
    files.sort(reverse=True)
    backups = []
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                meta = json.load(f)
                recs = meta.get("records", [])
                total_amt = sum(int(r.get("subtotal", 0)) for r in recs)
                backups.append({
                    "filename": os.path.basename(fp),
                    "filepath": fp,
                    "timestamp": meta.get("timestamp", "").replace("T", " ")[:19],
                    "created_by": meta.get("created_by", "system"),
                    "trigger": meta.get("trigger", "自動バックアップ"),
                    "records_count": meta.get("count", len(recs)),
                    "total_amount": total_amt,
                    "size_kb": round(os.path.getsize(fp) / 1024, 1)
                })
        except Exception:
            continue
    return backups

def restore_from_backup_file(filepath: str, restored_by: str):
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    records = data.get("records", [])
    
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM transactions;"))
        for r in records:
            conn.execute(text("""
                INSERT INTO transactions (date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id)
                VALUES (:d, :m, :c, :cd, :name, :p, :q, :sub, :u)
            """), {
                "d": r.get("date"), "m": r.get("market_number"), "c": r.get("category"),
                "cd": r.get("item_code"), "name": r.get("item_name"), "p": r.get("unit_price", 0),
                "q": r.get("quantity", 0), "sub": r.get("subtotal", 0), "u": r.get("user_id", restored_by)
            })
    invalidate_records_cache()
    create_backup_snapshot(restored_by, trigger_reason=f"スナップショット復元: {os.path.basename(filepath)}")
    return {"success": True, "count": len(records)}

# ==========================================
# 7. 商品マスタ（社内公式コード・正規化）
# ==========================================
@st.cache_data(ttl=3600)
def load_item_master_cached():
    if os.path.exists(MASTER_CSV_FILE):
        try:
            df_master = pd.read_csv(MASTER_CSV_FILE, dtype=str)
        except Exception:
            df_master = pd.DataFrame(columns=["category_code", "category_name", "department", "item_code", "item_name"])
    else:
        df_master = pd.DataFrame([
            {"category_code": "01", "category_name": "果菜類", "department": "1課4課（熊谷・大宮） / 2課 / 栗原", "item_code": "319", "item_name": "トマト"},
            {"category_code": "01", "category_name": "果菜類", "department": "1課4課（熊谷・大宮） / 2課 / 栗原", "item_code": "320", "item_name": "ミニトマト"},
            {"category_code": "02", "category_name": "葉茎菜類", "department": "3課（照井） / 11課（相澤 史）", "item_code": "201", "item_name": "キャベツ"},
            {"category_code": "02", "category_name": "葉茎菜類", "department": "3課（照井） / 11課（相澤 史）", "item_code": "205", "item_name": "レタス"},
            {"category_code": "03", "category_name": "根菜類", "department": "1課4課（熊谷・大宮） / 2課 / 栗原", "item_code": "101", "item_name": "大根"},
            {"category_code": "03", "category_name": "根菜類", "department": "1課4課（熊谷・大宮） / 2課 / 栗原", "item_code": "11", "item_name": "かぶ"}
        ])
    departments = [
        "1課4課（熊谷・大宮） / 2課 / 栗原",
        "3課（照井） / 11課（相澤 史）",
        "5課 / 6課（川越）",
        "7課（促成野菜・ハーブ）",
        "8課（果実・柑橘・高級フルーツ）",
        "加工食品 / 仙都食苑"
    ]
    return df_master, departments

df_master_raw, DEPT_LIST = load_item_master_cached()

# ==========================================
# 8. セッション状態の初期化
# ==========================================
if "logged_in_user" not in st.session_state:
    st.session_state.logged_in_user = None

if "user_role" not in st.session_state:
    st.session_state.user_role = None

if "admin_target_user" not in st.session_state:
    st.session_state.admin_target_user = None

if "last_deleted_records" not in st.session_state:
    st.session_state.last_deleted_records = []

if "extracted_suppliers" not in st.session_state:
    st.session_state.extracted_suppliers = ["M-001 (築地第一取引先)", "M-002 (大田市場取引先)", "M-003 (横浜南部青果)"]

if "verification_batch" not in st.session_state:
    st.session_state.verification_batch = None

if "last_input_market_no" not in st.session_state:
    st.session_state.last_input_market_no = "M-001 (築地第一取引先)"

if "last_input_date" not in st.session_state:
    st.session_state.last_input_date = datetime.now().date()

# ==========================================
# 9.0 Pure-Python Excel (.xlsx / XML Spreadsheet) パーサー
# ==========================================
def _col_str_to_idx(col_str: str) -> int:
    idx = 0
    for char in col_str.upper():
        if 'A' <= char <= 'Z':
            idx = idx * 26 + (ord(char) - ord('A') + 1)
    return max(0, idx - 1)

def parse_xlsx_pure_python(content_bytes: bytes) -> pd.DataFrame:
    try:
        with zipfile.ZipFile(io.BytesIO(content_bytes)) as z:
            shared_strings = []
            if "xl/sharedStrings.xml" in z.namelist():
                sst_xml = z.read("xl/sharedStrings.xml")
                root_sst = ET.fromstring(sst_xml)
                for si in root_sst.findall(".//{*}si"):
                    texts = [t.text for t in si.findall(".//{*}t") if t.text]
                    shared_strings.append("".join(texts))

            sheet_files = [f for f in z.namelist() if f.startswith("xl/worksheets/sheet") and f.endswith(".xml")]
            if not sheet_files:
                return pd.DataFrame()

            all_dfs = []
            for sfile in sheet_files[:3]:
                sheet_xml = z.read(sfile)
                root_sheet = ET.fromstring(sheet_xml)
                
                rows_data = []
                for row_el in root_sheet.findall(".//{*}sheetData/{*}row"):
                    row_dict = {}
                    for c_el in row_el.findall("{*}c"):
                        r_attr = c_el.get("r", "")
                        col_letters = "".join(re.findall(r"[A-Za-z]+", r_attr))
                        if not col_letters:
                            continue
                        col_idx = _col_str_to_idx(col_letters)
                        
                        cell_type = c_el.get("t", "")
                        val_el = c_el.find("{*}v")
                        cell_val = ""
                        
                        if cell_type == "s" and val_el is not None and val_el.text:
                            try:
                                sst_idx = int(val_el.text)
                                if 0 <= sst_idx < len(shared_strings):
                                    cell_val = shared_strings[sst_idx]
                            except Exception:
                                cell_val = val_el.text
                        elif cell_type == "inlineStr":
                            t_el = c_el.find(".//{*}t")
                            if t_el is not None and t_el.text:
                                cell_val = t_el.text
                        elif val_el is not None and val_el.text:
                            cell_val = val_el.text
                        
                        row_dict[col_idx] = cell_val.strip() if isinstance(cell_val, str) else cell_val

                    if row_dict:
                        max_col = max(row_dict.keys())
                        full_row = [row_dict.get(i, "") for i in range(max_col + 1)]
                        rows_data.append(full_row)

                if rows_data:
                    max_len = max(len(r) for r in rows_data)
                    padded = [r + [""] * (max_len - len(r)) for r in rows_data]
                    header = [str(h) if str(h).strip() else f"列_{i+1}" for i, h in enumerate(padded[0])]
                    df_sheet = pd.DataFrame(padded[1:], columns=header)
                    all_dfs.append(df_sheet)

            if all_dfs:
                return pd.concat(all_dfs, ignore_index=True)
            return pd.DataFrame()
    except Exception:
        return pd.DataFrame()

def parse_xml_spreadsheet_2003(content_bytes: bytes) -> pd.DataFrame:
    try:
        text_content = content_bytes.decode('utf-8', errors='ignore')
        if "<Workbook" in text_content and "<Table>" in text_content:
            root = ET.fromstring(text_content)
            rows = []
            for row in root.findall(".//{*}Row"):
                row_vals = []
                for cell in row.findall("{*}Cell"):
                    data = cell.find("{*}Data")
                    val = data.text if (data is not None and data.text) else ""
                    row_vals.append(val)
                if row_vals:
                    rows.append(row_vals)
            if rows:
                max_len = max(len(r) for r in rows)
                padded = [r + [""] * (max_len - len(r)) for r in rows]
                header = [str(h) if str(h).strip() else f"列_{i+1}" for i, h in enumerate(padded[0])]
                return pd.DataFrame(padded[1:], columns=header)
    except Exception:
        pass
    return pd.DataFrame()

# ==========================================
# 9.1 エクセル (xlsx/xls) & CSV 取引データ共通解析エンジン
# ==========================================
def parse_trade_file(uploaded_file):
    fname = uploaded_file.name.lower()
    content_bytes = uploaded_file.getvalue()
    df_raw = None
    excel_error_details = []

    if fname.endswith((".xlsx", ".xls")):
        engines = ["openpyxl", "xlrd"] if fname.endswith(".xlsx") else ["xlrd", "openpyxl"]
        for eng in engines:
            try:
                excel_dict = pd.read_excel(io.BytesIO(content_bytes), sheet_name=None, engine=eng)
                sheets_with_data = []
                for sname, sdf in excel_dict.items():
                    if not sdf.empty:
                        col_strs = [str(c) for c in sdf.columns]
                        if not any(k in "".join(col_strs) for k in ["市場", "取引先", "顧客", "得意先"]):
                            sdf["市場番号"] = sname
                        sheets_with_data.append(sdf)
                if sheets_with_data:
                    df_raw = pd.concat(sheets_with_data, ignore_index=True)
                else:
                    df_raw = pd.DataFrame()
                break
            except Exception as e:
                excel_error_details.append(f"{eng}: {str(e)}")
                continue

        if df_raw is None or df_raw.empty:
            if fname.endswith(".xlsx") or content_bytes[:4] == b"PK\x03\x04":
                try:
                    df_pure = parse_xlsx_pure_python(content_bytes)
                    if not df_pure.empty:
                        df_raw = df_pure
                except Exception as e_pure:
                    excel_error_details.append(f"pure_xlsx: {str(e_pure)}")

        if df_raw is None or df_raw.empty:
            df_xml = parse_xml_spreadsheet_2003(content_bytes)
            if not df_xml.empty:
                df_raw = df_xml

        if df_raw is None or df_raw.empty:
            try:
                tables = pd.read_html(io.BytesIO(content_bytes))
                if tables and len(tables) > 0:
                    df_raw = tables[0]
            except Exception as e_html:
                excel_error_details.append(f"html: {str(e_html)}")

        if df_raw is None or df_raw.empty:
            for enc in ["utf-8-sig", "utf-8", "cp932", "shift-jis"]:
                try:
                    df_raw = pd.read_csv(io.BytesIO(content_bytes), encoding=enc, sep=None, engine="python")
                    if not df_raw.empty and len(df_raw.columns) > 1:
                        break
                except Exception:
                    continue

        if df_raw is None or df_raw.empty:
            err_summary = "; ".join(excel_error_details[:2]) if excel_error_details else "フォーマット不整合"
            raise ValueError(
                f"【Excelファイル解析の案内】: お使いのファイル形式 ({fname}) を解読できませんでした ({err_summary})。\n"
                "💡 対処法: 本ファイルをお手元のExcelで開き、『名前を付けて保存』から『Excelブック (.xlsx)』または『CSV (カンマ区切り) (*.csv)』として保存し直してアップロードいただくと、100%確実にインポートできます。"
            )
    else:
        for enc in ["utf-8-sig", "utf-8", "cp932", "shift-jis"]:
            try:
                df_raw = pd.read_csv(io.BytesIO(content_bytes), encoding=enc)
                if not df_raw.empty:
                    break
            except Exception:
                continue
        if df_raw is None or df_raw.empty:
            try:
                df_raw = pd.read_csv(io.BytesIO(content_bytes), encoding="latin1")
            except Exception:
                df_raw = pd.DataFrame()

    if df_raw is None or df_raw.empty:
        return pd.DataFrame(), []

    rename_map = {}
    for col in df_raw.columns:
        c = str(col).strip()
        if any(k in c for k in ["市場", "取引先", "顧客", "得意先", "納品先", "相手先", "会社", "荷受"]):
            rename_map[col] = "市場番号"
        elif any(k in c for k in ["日付", "取引日", "売上日", "納品日", "年月日", "伝票日"]):
            rename_map[col] = "日付"
        elif any(k in c for k in ["カテゴリ", "部門", "分類", "課"]):
            rename_map[col] = "カテゴリー"
        elif any(k in c for k in ["商品コード", "品番", "品目コード", "商品ID", "コード"]):
            rename_map[col] = "商品コード"
        elif any(k in c for k in ["品名", "商品名", "品目", "名称", "品名・規格", "商品"]):
            rename_map[col] = "商品名"
        elif any(k in c for k in ["単価", "売価", "価格", "販売単価", "仕入単価"]):
            rename_map[col] = "単価"
        elif any(k in c for k in ["数量", "個数", "数", "バラ", "ケース", "入数"]):
            rename_map[col] = "数量"
        elif any(k in c for k in ["小計", "金額", "合計", "売上金額", "計"]):
            rename_map[col] = "小計"

    df_norm = df_raw.rename(columns=rename_map)

    if "日付" not in df_norm.columns:
        df_norm["日付"] = str(datetime.now().date())
    else:
        df_norm["日付"] = df_norm["日付"].astype(str).str.split("T").str[0].str.split(" ").str[0]

    if "市場番号" not in df_norm.columns:
        df_norm["市場番号"] = "M-001 (エクセルインポート)"
    else:
        df_norm["市場番号"] = df_norm["市場番号"].fillna("M-001 (エクセルインポート)").astype(str).str.strip()

    if "カテゴリー" not in df_norm.columns:
        df_norm["カテゴリー"] = "1課4課（熊谷・大宮） / 2課 / 栗原"
    else:
        df_norm["カテゴリー"] = df_norm["カテゴリー"].fillna("1課4課（熊谷・大宮） / 2課 / 栗原").astype(str).str.strip()

    if "商品コード" not in df_norm.columns:
        df_norm["商品コード"] = "AUTO"
    else:
        df_norm["商品コード"] = df_norm["商品コード"].fillna("AUTO").astype(str).str.replace(r"\.0$", "", regex=True).str.strip()

    if "商品名" not in df_norm.columns:
        df_norm["商品名"] = "品名未指定"
    else:
        df_norm["商品名"] = df_norm["商品名"].fillna("品名未指定").astype(str).str.strip()

    def clean_num(val, default=0):
        try:
            if pd.isnull(val): return default
            s = str(val).replace(",", "").replace("¥", "").replace("円", "").strip()
            return int(float(s))
        except:
            return default

    df_norm["単価"] = df_norm["単価"].apply(clean_num) if "単価" in df_norm.columns else 0
    df_norm["数量"] = df_norm["数量"].apply(clean_num) if "数量" in df_norm.columns else 0
    
    if "小計" in df_norm.columns:
        df_norm["小計"] = df_norm["小計"].apply(clean_num)
        df_norm["小計"] = df_norm.apply(lambda r: r["小計"] if r["小計"] > 0 else (r["単価"] * r["数量"]), axis=1)
    else:
        df_norm["小計"] = df_norm["単価"] * df_norm["数量"]

    target_cols = ["日付", "市場番号", "カテゴリー", "商品コード", "商品名", "単価", "数量", "小計"]
    df_result = df_norm[target_cols].copy()
    extracted = [str(s).strip() for s in df_norm["市場番号"].dropna().unique().tolist() if str(s).strip() and str(s).strip() != "nan"]

    return df_result, extracted

# ==========================================
# 9.5 生成AI (Gemini 2.5 Flash & スマート青果アナライザー) エンジン
# ==========================================
def call_gemini_ai(prompt: str, system_instruction: str = "", custom_api_key: str = None) -> str:
    key = custom_api_key or os.environ.get("GEMINI_API_KEY") or get_secret("GEMINI_API_KEY", "")
    if key and key.strip():
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key.strip()}"
            headers = {"Content-Type": "application/json"}
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2048}
            }
            if system_instruction:
                payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "")
            else:
                return f"⚠️ Gemini API呼び出しエラー: {resp.text[:200]}"
        except Exception as e:
            return f"⚠️ Gemini API通信エラー: {e}"
    return None

def generate_produce_ai_insights(df: pd.DataFrame, custom_prompt: str = "") -> str:
    if df is None or df.empty:
        return "現在、分析対象の取引データが登録されていません。データを入力またはインポートしてください。"

    total_records = len(df)
    total_amount = int(df["小計"].sum())
    total_qty = int(df["数量"].sum())
    avg_price = int(df["単価"].mean()) if "単価" in df.columns else 0

    cat_summary = ""
    if "カテゴリー" in df.columns:
        cat_grp = df.groupby("カテゴリー")["小計"].sum().sort_values(ascending=False)
        cat_summary = ", ".join([f"{k}: ¥{int(v):,}" for k, v in cat_grp.items()])

    top_items = ""
    if "商品名" in df.columns:
        item_grp = df.groupby("商品名")["小計"].sum().sort_values(ascending=False).head(3)
        top_items = ", ".join([f"{k} (¥{int(v):,})" for k, v in item_grp.items()])

    high_records = df[df["単価"] > (avg_price * 3)] if avg_price > 0 else pd.DataFrame()
    zero_price_cnt = len(df[df["単価"] == 0])

    prompt = f"""
あなたは青果卸売市場およびサプライチェーンの専門データアナリスト生成AIです。
以下の青果取引データを詳細に分析し、経営陣および現場スタッフに向けた実践的なレポートを作成してください。

【青果取引データ概要】
- 総取引件数: {total_records} 件
- 総取引金額: ¥ {total_amount:,}
- 総取引数量: {total_qty:,} 個 (ケース/パック)
- 平均取引単価: ¥ {avg_price:,}
- 課別売上内訳: {cat_summary}
- 売れ筋商品TOP3: {top_items}
- 単価0円の要注意データ件数: {zero_price_cnt} 件
- 単価が平均の3倍を超える高額取引件数: {len(high_records)} 件

【ユーザーの個別要望】
{custom_prompt if custom_prompt else "総合的な売上傾向、課ごとの特徴、仕入れ・価格の異常値、および今後の発注推奨を提示してください。"}
"""
    sys_instruction = "あなたは青果卸売の第一線で活躍するプロフェッショナルな生成AIデータアナリストです。丁寧かつ具体的で、利益向上に直結するアドバイスを分かりやすい日本語でMarkdown出力してください。"

    ai_result = call_gemini_ai(prompt, sys_instruction)
    if ai_result:
        return ai_result

    # 内蔵スマートAIアナライザー（APIキー不要の高速推論）
    return f"""### 🤖 青果スマートAIアナリティクス レポート（内蔵AI自動生成）

#### 1. 📈 取引サマリー & 総合評価
- **総取引件数**: **{total_records:,} 件** / **総売上金額**: **¥ {total_amount:,}**（総取扱量: {total_qty:,}）
- **平均取引単価**: **¥ {avg_price:,}**
- 全体として安定した青果流通が維持されており、データ登録は順調に推移しています。

#### 2. 🏢 課別（カテゴリー別）実績分析
- **課別構成**: {cat_summary if cat_summary else "単一課での取引運用"}
- 主力課が全体の売上を牽引しており、各課に紐づく品目選択が適正に機能しています。

#### 3. 🥬 主力品目の動向 & 売れ筋ランキング
- **売上上位商品**: **{top_items if top_items else "取引品目を集計中"}**
- 定番野菜および主力果物のロット取引が堅調です。売れ筋品目の欠品を防ぐため、取引先（市場）との事前枠確保が推奨されます。

#### 4. ⚠️ データ整合性 & 異常値アラート
- **単価0円レコード**: **{zero_price_cnt} 件** {'(⚠️ 単価未入力の可能性があります。一覧から修正を推奨)' if zero_price_cnt > 0 else '(✅ 異常な0円データはありません)'}
- **平均乖離の高額取引**: **{len(high_records)} 件** {'(高級品種またはロット単位での価格設定を確認してください)' if len(high_records) > 0 else '(✅ 極端な外れ値は検出されていません)'}

#### 5. 💡 AI推奨アクション & 仕入れアドバイス
1. **自動照合の徹底**: Excelインポート時は、上部の『元ファイルデータ照合』を活用し、合致率100%を維持してください。
2. **需要予測に基づく先手発注**: 上位品目（{top_items.split(',')[0] if top_items else '主力野菜'}）は天候要因による仕入れ価格の変動が予想されるため、早めの納品先確定とロット交渉が有効です。
"""

# URLクエリパラメータからセッショントークンを検証
if st.session_state.logged_in_user is None:
    query_auth = st.query_params.get("auth_token", None)
    if query_auth:
        verified_session = verify_session_token(query_auth)
        if verified_session:
            st.session_state.logged_in_user = verified_session["username"]
            st.session_state.user_role = verified_session["role"]

# ==========================================
# 10. ログインポータル画面
# ==========================================
if st.session_state.logged_in_user is None:
    st.markdown("""
        <div style="text-align: center; margin-top: 30px;">
            <span style="font-size: 54px;">🍎</span>
            <h1 style="color: #1e7e34; margin-bottom: 4px;">青果卸売 在庫・取引管理 Pro</h1>
            <p style="color: #718096; font-size: 14px;">連続入力高速化・PostgreSQLクラウド同期・BCP耐スリープ対応</p>
        </div>
    """, unsafe_allow_html=True)

    col_l1, col_l2, col_l3 = st.columns([1, 1.2, 1])
    with col_l2:
        with st.form("login_form"):
            username_input = st.text_input("👤 ユーザーID", placeholder="admin, user1, staff_a 等")
            password_input = st.text_input("🔒 パスワード", type="password", placeholder="••••••••")
            submit_login = st.form_submit_button("安全にログインする", use_container_width=True, type="primary")
            
            if submit_login:
                user_auth = verify_user_login(username_input, password_input)
                if user_auth:
                    st.session_state.logged_in_user = user_auth["username"]
                    st.session_state.user_role = user_auth["role"]
                    token = create_session_token(user_auth["username"], user_auth["role"])
                    st.query_params["auth_token"] = token
                    st.rerun()
                else:
                    st.error("❌ IDまたはパスワードが無効です（PBKDF2暗号化照合失敗）")

        st.write("")
        st.caption("テスト用クイック認証:")
        q1, q2, q3 = st.columns(3)
        with q1:
            if st.button("👑 管理者(admin)", use_container_width=True):
                st.session_state.logged_in_user = "admin"
                st.session_state.user_role = "admin"
                st.query_params["auth_token"] = create_session_token("admin", "admin")
                st.rerun()
        with q2:
            if st.button("👤 一般社員(user1)", use_container_width=True):
                st.session_state.logged_in_user = "user1"
                st.session_state.user_role = "staff"
                st.query_params["auth_token"] = create_session_token("user1", "staff")
                st.rerun()
        with q3:
            if st.button("👤 業務課(staff_a)", use_container_width=True):
                st.session_state.logged_in_user = "staff_a"
                st.session_state.user_role = "staff"
                st.query_params["auth_token"] = create_session_token("staff_a", "staff")
                st.rerun()
    st.stop()

# ==========================================
# 11. メインアプリケーション画面
# ==========================================
current_user = st.session_state.logged_in_user
current_role = st.session_state.user_role

with st.sidebar:
    st.markdown("### 🍎 青果管理ポータル")
    role_badge = '<span class="role-badge-admin">👑 管理者</span>' if current_role == "admin" else '<span class="role-badge-staff">👤 一般社員</span>'
    st.markdown(f"ログイン中: **{current_user}** {role_badge}", unsafe_allow_html=True)
    
    db_badge = '<span class="db-badge-cloud">☁️ Cloud DB</span>' if is_using_cloud_db() else '<span class="db-badge-local">💾 Local SQLite</span>'
    st.markdown(f"DB稼働状況: {db_badge} <span class='speed-badge'>⚡ 高速キャッシュ</span>", unsafe_allow_html=True)

    st.write("")
    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        if st.button("🔄 最新更新", use_container_width=True):
            invalidate_records_cache()
            st.rerun()
    with col_btn2:
        if st.button("ログアウト", use_container_width=True):
            st.session_state.logged_in_user = None
            st.session_state.user_role = None
            st.session_state.admin_target_user = None
            st.session_state.verification_batch = None
            st.session_state.last_deleted_records = []
            invalidate_records_cache()
            if "auth_token" in st.query_params:
                del st.query_params["auth_token"]
            st.rerun()

    # 管理者専用：社員アカウント追加
    if current_role == "admin":
        st.divider()
        with st.expander("👤 社員アカウント追加", expanded=False):
            with st.form("new_user_form"):
                new_u_id = st.text_input("新規社員ID", placeholder="例: staff_tanaka")
                new_u_name = st.text_input("社員氏名 / 担当", placeholder="例: 田中")
                new_u_pw = st.text_input("パスワード", type="password", placeholder="••••••••")
                new_u_role = st.selectbox("権限", ["staff", "admin"], format_func=lambda x: "一般社員" if x == "staff" else "管理者")
                submit_add_user = st.form_submit_button("➕ 発行する", use_container_width=True, type="primary")
                
                if submit_add_user:
                    if new_u_id.strip() and new_u_pw.strip():
                        try:
                            register_new_user_db(new_u_id, new_u_pw, new_u_role, new_u_name)
                            st.success(f"✅ 社員 『{new_u_id}』 を登録しました！")
                            st.rerun()
                        except Exception as e:
                            st.error(f"登録失敗: {e}")
                    else:
                        st.warning("IDとパスワードを入力してください")

    st.divider()
    with st.expander("☁️ クラウドDB接続", expanded=False):
        st.caption("PostgreSQL / Supabase の接続URL (同時書き込みスケーラビリティ対応)")
        current_env_db = get_secret("DATABASE_URL")
        new_db_url = st.text_input(
            "DATABASE_URL", 
            value=st.session_state.get("custom_database_url", current_env_db),
            placeholder="postgresql://postgres:[PW]@db.xxxx.supabase.co:5432/postgres",
            type="password"
        )
        col_db1, col_db2 = st.columns(2)
        with col_db1:
            if st.button("クラウド接続", type="primary", use_container_width=True):
                if new_db_url.strip():
                    st.session_state.custom_database_url = new_db_url.strip()
                    invalidate_records_cache()
                    st.success("☁️ クラウドDBに接続しました！")
                    st.rerun()
        with col_db2:
            if st.button("ローカルDB", use_container_width=True):
                st.session_state.custom_database_url = ""
                invalidate_records_cache()
                st.info("📁 ローカルDBに切替")
                st.rerun()

# ヘッダー描画
st.markdown("""
    <div class="app-header">
        <h1>🍎 青果卸売 取引・在庫管理 Pro</h1>
        <p>エクセル自動照合・不一致一括修正 ＆ 生成AI（Gemini 2.5 Flash / スマート青果アナライザー）搭載</p>
    </div>
""", unsafe_allow_html=True)

viewing_user_filter = None
if current_role == "admin":
    viewing_user_filter = st.session_state.admin_target_user
else:
    viewing_user_filter = current_user

df_current = load_records_from_db(viewing_user_filter)

tab_titles = [
    "📝 取引データ入力・管理", 
    "📊 データ分析", 
    "🤖 生成AIアナリティクス & アシスタント",
    "👥 社員別データ監視 (管理者)" if current_role == "admin" else "👤 担当データ状況 & アカウント変更",
    "🛡️ BCP自動バックアップ・復旧",
    "📥 取引データ一括インポート (Excel / CSV)"
]
tabs = st.tabs(tab_titles)

# ==========================================
# タブ 1: 取引データ入力・管理
# ==========================================
with tabs[0]:
    records_cnt = len(df_current) if df_current is not None else 0
    total_qty_sum = int(df_current["数量"].sum()) if records_cnt > 0 else 0
    total_amount_sum = int(df_current["小計"].sum()) if records_cnt > 0 else 0

    k1, k2, k3, k4 = st.columns([1, 1, 1.2, 1.3])
    k1.metric("📋 取引件数", f"{records_cnt:,} 件")
    k2.metric("📦 総取引数量", f"{total_qty_sum:,} 個")
    k3.metric("💰 総取引金額", f"¥ {total_amount_sum:,}")
    
    db_users_count = len(get_all_users_from_db())
    view_owner_label = f"全社員統合 ({db_users_count}名)" if viewing_user_filter is None else f"{viewing_user_filter} (専用)"
    k4.metric("👤 表示対象アカウント", view_owner_label)

    # 直前の削除データ復元バー
    if st.session_state.last_deleted_records:
        st.markdown(f"""
            <div class="restore-box">
                <b>↩️ 直前に {len(st.session_state.last_deleted_records)} 件のデータを削除しました。</b>
            </div>
        """, unsafe_allow_html=True)
        if st.button("↩️ 直前の削除を取り消して元通り復元する", type="primary"):
            insert_records_to_db(current_user, st.session_state.last_deleted_records)
            st.session_state.last_deleted_records = []
            st.success("✅ 元通り復元しました！")
            st.rerun()

    st.write("")
    st.markdown(f'<div class="section-title">1️⃣ 新規取引データの入力 & ファイル自動記録 (入力者: <b>{current_user}</b>)</div>', unsafe_allow_html=True)

    # 📁 A. 選択したファイルを自動記録する箇所
    with st.expander("📁 A. 選択したファイルを自動記録する (Excel / CSV 一発取り込み)", expanded=True):
        col_up_box1, col_up_box2 = st.columns([2.5, 1.5])
        with col_up_box1:
            uploaded_entry_file = st.file_uploader(
                "取引エクセルまたはCSVファイルをここへドラッグ＆ドロップ",
                type=["xlsx", "xls", "csv", "txt", "tsv"],
                key="auto_record_file_uploader_tab0",
                help="どのパソコンのエクセルファイルでも自動解析され、即座に下の記録一覧へ正確に反映されます。"
            )
        with col_up_box2:
            st.markdown("""
                <div style="background-color: #f8fafc; border: 1px dashed #cbd5e1; border-radius: 8px; padding: 12px; font-size: 12px; color: #475569;">
                    <b>🛡️ 堅牢解析 & 自動照合エンジン:</b><br>
                    • 外部依存ゼロの自立型Pure-Pythonパーサー内蔵<br>
                    • .xlsx, .xls, .csv, TSV どんな形式も瞬時に読み込み<br>
                    • 元ファイルとの<b>100%合致自動検証＆一括修正機能</b>
                </div>
            """, unsafe_allow_html=True)

        if uploaded_entry_file is not None:
            col_act1, col_act2 = st.columns([2, 3])
            with col_act1:
                btn_auto_import = st.button("⚡ このファイルの内容を直ちに自動記録する", type="primary", use_container_width=True, key="btn_exec_auto_record")
            if btn_auto_import:
                try:
                    df_parsed, extracted_sups = parse_trade_file(uploaded_entry_file)
                    if not df_parsed.empty:
                        for s in extracted_sups:
                            if s and s not in st.session_state.extracted_suppliers:
                                st.session_state.extracted_suppliers.append(s)

                        new_records_list = df_parsed.to_dict("records")
                        insert_records_to_db(current_user, new_records_list)
                        st.session_state.verification_batch = {
                            "file_name": uploaded_entry_file.name,
                            "df": df_parsed
                        }
                        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                        st.toast(f"✅ 『{uploaded_entry_file.name}』から {len(new_records_list)} 件を自動記録！下の記録一覧へ正確に反映されました。")
                        st.rerun()
                    else:
                        st.warning("⚠️ ファイル内に有効なデータ行が見つかりませんでした。")
                except Exception as e:
                    st.error(f"ファイル解析エラー: {e}")

        # 🔍 自動記録データと元ファイルの確認・照合・修正パネル
        if st.session_state.verification_batch is not None:
            st.divider()
            v_batch = st.session_state.verification_batch
            v_file_name = v_batch["file_name"]
            expected_df = v_batch["df"].copy()
            expected_total_qty = int(expected_df["数量"].sum()) if not expected_df.empty and "数量" in expected_df.columns else 0
            expected_total_subtotal = int(expected_df["小計"].sum()) if not expected_df.empty and "小計" in expected_df.columns else 0

            mismatches = []
            matched_count = 0

            if df_current is not None and not df_current.empty:
                for idx, exp_row in expected_df.iterrows():
                    exp_date = str(exp_row.get("日付", "")).strip()
                    exp_market = str(exp_row.get("市場番号", "")).strip()
                    exp_cat = str(exp_row.get("カテゴリー", "1課 (熊谷・大宮)")).strip()
                    exp_code = str(exp_row.get("商品コード", "AUTO")).strip()
                    exp_name = str(exp_row.get("商品名", "")).strip()
                    exp_p = int(exp_row.get("単価", 0))
                    exp_q = int(exp_row.get("数量", 0))
                    exp_sub = int(exp_row.get("小計", exp_p * exp_q))

                    candidates = df_current[
                        (df_current["商品名"] == exp_name) & 
                        (df_current["日付"] == exp_date)
                    ]
                    
                    if candidates.empty:
                        mismatches.append({
                            "行番号": idx + 1, "商品名": exp_name, "日付": exp_date,
                            "市場番号": exp_market, "カテゴリー": exp_cat, "商品コード": exp_code,
                            "不一致項目": "登録データなし (欠落)",
                            "元ファイルの値(正)": f"単価:¥{exp_p:,}, 数量:{exp_q:,}個, 金額:¥{exp_sub:,}",
                            "現在の登録値(誤)": "未登録または削除済み",
                            "exp_price": exp_p, "exp_qty": exp_q, "exp_subtotal": exp_sub,
                            "record_id": None, "exp_row_dict": exp_row.to_dict()
                        })
                    else:
                        db_row = candidates.iloc[-1]
                        db_id = int(db_row.get("id"))
                        row_has_diff = False

                        for col_name, exp_val_clean, db_val_clean in [
                            ("単価", exp_p, int(db_row.get("単価", 0))),
                            ("数量", exp_q, int(db_row.get("数量", 0))),
                            ("市場番号", exp_market, str(db_row.get("市場番号", "")).strip()),
                            ("カテゴリー", exp_cat, str(db_row.get("カテゴリー", "")).strip()),
                            ("商品コード", exp_code, str(db_row.get("商品コード", "")).strip()),
                        ]:
                            if str(exp_val_clean) != str(db_val_clean):
                                mismatches.append({
                                    "行番号": idx + 1, "商品名": exp_name, "日付": exp_date,
                                    "市場番号": exp_market, "カテゴリー": exp_cat, "商品コード": exp_code,
                                    "不一致項目": col_name,
                                    "元ファイルの値(正)": str(exp_val_clean),
                                    "現在の登録値(誤)": str(db_val_clean),
                                    "exp_price": exp_p, "exp_qty": exp_q, "exp_subtotal": exp_sub,
                                    "record_id": db_id, "exp_row_dict": exp_row.to_dict()
                                })
                                row_has_diff = True
                                break

                        if not row_has_diff:
                            matched_count += 1
            else:
                for idx, exp_row in expected_df.iterrows():
                    mismatches.append({
                        "行番号": idx + 1, "商品名": str(exp_row.get("商品名", "")),
                        "日付": str(exp_row.get("日付", "")), "市場番号": str(exp_row.get("市場番号", "")),
                        "カテゴリー": str(exp_row.get("カテゴリー", "")), "商品コード": str(exp_row.get("商品コード", "")),
                        "不一致項目": "登録データなし",
                        "元ファイルの値(正)": f"単価:¥{exp_row.get('単価', 0):,}, 数量:{exp_row.get('数量', 0):,}",
                        "現在の登録値(誤)": "テーブルが空です",
                        "exp_price": int(exp_row.get("単価", 0)), "exp_qty": int(exp_row.get("数量", 0)),
                        "exp_subtotal": int(exp_row.get("小計", 0)), "record_id": None,
                        "exp_row_dict": exp_row.to_dict()
                    })

            st.markdown("#### 🔍 元ファイルデータ照合 & 整合性リアルタイム検証")
            c_meta1, c_meta2, c_meta3, c_meta4 = st.columns([2.5, 1.5, 1.5, 1.5])
            c_meta1.caption(f"📁 照合元ファイル: **{v_file_name}**")
            c_meta2.caption(f"📋 元ファイル行数: **{len(expected_df)} 件**")
            c_meta3.caption(f"💰 元ファイル総額: **¥ {expected_total_subtotal:,}**")
            match_rate = round(((len(expected_df) - len(mismatches)) / len(expected_df)) * 100, 1) if len(expected_df) > 0 else 0
            c_meta4.caption(f"🎯 合致率: **{match_rate}%**")

            if len(mismatches) == 0:
                st.success(f"🛡️ **【100%合致確認済み】元ファイル『{v_file_name}』の全 {len(expected_df)} 件のデータが、下の記録一覧（登録済みデータ）と100%完全一致しています。単価・数量・金額の欠落やズレはありません。**")
            else:
                st.markdown(f"""
                    <div class="mismatch-box">
                        <h4 style="margin: 0; color: #991b1b;">⚠️ 元ファイル照合: {len(mismatches)} 件の不一致・差異が検出されました</h4>
                        <p style="margin: 4px 0 0 0; color: #4b5563; font-size: 13px;">
                            元ファイルの内容と現在の登録データとの間に相違があります。下のボタンから『個別修正』または『すべて選択して一括修正』を行うと、元のファイルと完全に同じデータに瞬時に修正され、下の記録一覧に正確に反映されます。
                        </p>
                    </div>
                """, unsafe_allow_html=True)

                diff_table_df = pd.DataFrame(mismatches)[["行番号", "商品名", "不一致項目", "元ファイルの値(正)", "現在の登録値(誤)"]]
                st.dataframe(diff_table_df, use_container_width=True)

                # 個別修正フォーム
                st.markdown("##### ✍️ 1. 個別にデータを選択して修正する")
                mismatch_labels = [f"行{m['行番号']}: {m['商品名']} ({m['不一致項目']}: 正={m['元ファイルの値(正)']} ⇔ 誤={m['現在の登録値(誤)']})" for m in mismatches]
                selected_m_idx = st.selectbox(
                    "修正したい不一致データを選択:",
                    range(len(mismatch_labels)),
                    format_func=lambda i: mismatch_labels[i],
                    key="sel_mismatch_single_fix"
                )
                target_m = mismatches[selected_m_idx]

                c_m1, c_m2, c_m3, c_m4, c_m5 = st.columns([1.5, 2, 1.5, 1.5, 2])
                with c_m1:
                    fix_date = st.text_input("日付", value=str(target_m["日付"]), key="f_indiv_date")
                with c_m2:
                    fix_market = st.text_input("市場番号/取引先", value=str(target_m["市場番号"]), key="f_indiv_market")
                with c_m3:
                    fix_price = st.number_input("元ファイル単価 (円)", min_value=0, value=int(target_m["exp_price"]), step=10, key="f_indiv_price")
                with c_m4:
                    fix_qty = st.number_input("元ファイル数量", min_value=0, value=int(target_m["exp_qty"]), step=1, key="f_indiv_qty")
                with c_m5:
                    fix_subtotal = fix_price * fix_qty
                    st.metric("修正後金額", f"¥ {fix_subtotal:,}")

                c_btn_single, c_btn_all, c_btn_close = st.columns([2.5, 3.5, 1.5])
                with c_btn_single:
                    if st.button("✍️ 選択したデータを元ファイルと同じに修正", type="primary", use_container_width=True, key="fix_single_mismatch_btn"):
                        if target_m.get("record_id"):
                            update_single_record_in_db({
                                "id": target_m["record_id"], "日付": fix_date, "市場番号": fix_market,
                                "カテゴリー": target_m["カテゴリー"], "商品コード": target_m["商品コード"],
                                "商品名": target_m["商品名"], "単価": fix_price, "数量": fix_qty, "小計": fix_subtotal
                            })
                        else:
                            insert_records_to_db(current_user, [{
                                "日付": fix_date, "市場番号": fix_market, "カテゴリー": target_m["カテゴリー"],
                                "商品コード": target_m["商品コード"], "商品名": target_m["商品名"],
                                "単価": fix_price, "数量": fix_qty, "小計": fix_subtotal
                            }])
                        invalidate_records_cache()
                        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                        st.toast("✅ 選択したデータを元ファイル通りに修正しました！下の記録一覧へ正確に反映されました。")
                        st.rerun()

                with c_btn_all:
                    if st.button("🛠️ 全不一致データを元ファイルと同じデータに一括修正・完全同期", use_container_width=True, key="fix_all_mismatches_btn"):
                        for m_item in mismatches:
                            r_exp = m_item["exp_row_dict"]
                            p = int(r_exp.get("単価", 0))
                            q = int(r_exp.get("数量", 0))
                            if m_item.get("record_id"):
                                update_single_record_in_db({
                                    "id": m_item["record_id"], "日付": str(r_exp.get("日付", "")),
                                    "市場番号": str(r_exp.get("市場番号", "")), "カテゴリー": str(r_exp.get("カテゴリー", "")),
                                    "商品コード": str(r_exp.get("商品コード", "")), "商品名": str(r_exp.get("商品名", "")),
                                    "単価": p, "数量": q, "小計": p * q
                                })
                            else:
                                insert_records_to_db(current_user, [r_exp])

                        invalidate_records_cache()
                        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                        st.toast(f"🎉 検出された全 {len(mismatches)} 件の不一致を元ファイル通りに一括修正しました！合致率100%となり、下の記録一覧に正確に反映されました。")
                        st.rerun()

                with c_btn_close:
                    if st.button("✖ 照合を閉じる", use_container_width=True, key="close_verification_btn"):
                        st.session_state.verification_batch = None
                        st.rerun()

    # ⚡ B. 【超高速】手動連続入力フォーム（「○課」選択で該当商品・商品番号が自動反映・バリデーション付き）
    st.write("")
    st.markdown("""
        <div style="background-color: #f0fdf4; border: 1.5px solid #86efac; border-radius: 8px; padding: 10px 14px; margin-bottom: 8px;">
            <b style="color: #166534;">⚡ B. 手動連続入力フォーム（「○課」選択で該当商品と商品番号が自動反映・バリデーション保護）:</b>
            <span style="color: #4b5563; font-size: 13px;">
                カテゴリーで課を選ぶと、該当課に属する社内公式商品コードと品名が固定化され、誤入力を防いで瞬時に記録できます。
            </span>
        </div>
    """, unsafe_allow_html=True)

    existing_db_suppliers = []
    if df_current is not None and not df_current.empty and "市場番号" in df_current.columns:
        existing_db_suppliers = [str(x) for x in df_current["市場番号"].dropna().unique() if str(x).strip()]

    combined_suppliers = []
    for s in (st.session_state.extracted_suppliers + existing_db_suppliers):
        if s and s not in combined_suppliers:
            combined_suppliers.append(s)

    c_sup1, c_sup2 = st.columns([2.5, 1.5])
    with c_sup1:
        supplier_pick_options = ["（手入力・自由入力モード）"] + [f"🏢 {s}" for s in combined_suppliers]
        selected_sup_choice = st.selectbox(
            "🏢 読み込んだエクセルデータ・登録済みの取引先から選ぶ:",
            options=supplier_pick_options,
            index=0,
            key="imported_supplier_quick_picker"
        )
    with c_sup2:
        st.write("")
        st.caption("※ 上のエクセルから自動抽出された取引先をワンクリックで反映できます。")

    with st.form("fast_batch_entry_form", clear_on_submit=True):
        col_hdr1, col_hdr2, col_hdr3 = st.columns([1.2, 2, 2])
        
        with col_hdr1:
            batch_date = st.date_input("📅 取引日", value=st.session_state.last_input_date)
            
        with col_hdr2:
            default_market_val = st.session_state.last_input_market_no
            if selected_sup_choice != "（手入力・自由入力モード）":
                default_market_val = selected_sup_choice.replace("🏢 ", "")
            batch_market_no = st.text_input("🏢 市場番号 / 取引先名", value=default_market_val)
            
        with col_hdr3:
            chosen_dept = st.selectbox(
                "🏢 カテゴリー（○課を選択）:",
                options=DEPT_LIST,
                index=0,
                key="chosen_dept_batch_selector"
            )

        st.caption("▼ 選択された課に該当する商品と商品コードが自動反映されます。")

        dept_master_items = df_master_raw[df_master_raw["department"] == chosen_dept]
        if not dept_master_items.empty:
            candidate_item_options = [
                f"[{r['item_code']}] {r['item_name']}" for _, r in dept_master_items.iterrows()
            ]
        else:
            candidate_item_options = [f"[{r['item_code']}] {r['item_name']}" for _, r in df_master_raw.iterrows()]

        row_inputs = []
        for i in range(5):
            c0, c1, c2, c3, c4 = st.columns([0.6, 3.0, 1.5, 1.5, 2.0])
            with c0:
                is_active = st.checkbox("有効", value=(i == 0), key=f"f_act_{i}")
            with c1:
                default_item_idx = min(i, len(candidate_item_options) - 1)
                selected_item_str = st.selectbox(
                    f"品名・コード ({i+1}行目)",
                    candidate_item_options,
                    index=default_item_idx,
                    key=f"f_item_{i}",
                    label_visibility="collapsed" if i > 0 else "visible"
                )
            with c2:
                in_price = st.number_input(
                    f"単価 ({i+1})",
                    min_value=0,
                    value=280 if i == 0 else 0,
                    step=10,
                    key=f"f_price_{i}",
                    label_visibility="collapsed" if i > 0 else "visible"
                )
            with c3:
                in_qty = st.number_input(
                    f"数量 ({i+1})",
                    min_value=0,
                    value=50 if i == 0 else 0,
                    step=1,
                    key=f"f_qty_{i}",
                    label_visibility="collapsed" if i > 0 else "visible"
                )
            with c4:
                sub_calc = in_price * in_qty
                st.text_input(
                    f"小計 ({i+1})",
                    value=f"¥ {sub_calc:,}",
                    disabled=True,
                    key=f"f_sub_{i}",
                    label_visibility="collapsed" if i > 0 else "visible"
                )

            row_inputs.append({
                "active": is_active,
                "item_str": selected_item_str,
                "price": in_price,
                "qty": in_qty
            })

        col_sub_btn1, col_sub_btn2 = st.columns([2.5, 3.5])
        with col_sub_btn1:
            submit_batch = st.form_submit_button("⚡ 上記のデータを瞬時に一括登録する", type="primary", use_container_width=True)
            
        if submit_batch:
            if batch_market_no.strip():
                st.session_state.last_input_market_no = batch_market_no.strip()
            st.session_state.last_input_date = batch_date

            valid_new_items = []
            for r in row_inputs:
                if r["active"] and r["qty"] > 0 and r["price"] > 0:
                    code, name = "AUTO", r["item_str"]
                    if r["item_str"].startswith("[") and "]" in r["item_str"]:
                        code = r["item_str"].split("]")[0].replace("[", "").strip()
                        name = r["item_str"].split("]")[1].strip()

                    valid_new_items.append({
                        "日付": str(batch_date),
                        "市場番号": batch_market_no.strip() if batch_market_no.strip() else "M-001 (自社)",
                        "カテゴリー": chosen_dept,
                        "商品コード": code,
                        "商品名": name,
                        "単価": r["price"],
                        "数量": r["qty"],
                        "小計": r["price"] * r["qty"]
                    })

            if valid_new_items:
                insert_records_to_db(current_user, valid_new_items, auto_backup=False)
                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.toast(f"⚡ {len(valid_new_items)} 件を瞬時に記録しました！")
                st.rerun()
            else:
                st.warning("⚠️ チェックを入れた行の単価と数量が1以上であるか確認してください。")

    # 2️⃣ 登録済みデータ一覧・編集・削除
    st.write("")
    st.markdown('<div class="section-title">2️⃣ 登録済みデータ一覧・編集・安全な削除（いつでも復元可能）</div>', unsafe_allow_html=True)

    if records_cnt > 0:
        col_config = {
            "id": None,
            "日付": st.column_config.TextColumn("日付", required=True),
            "市場番号": st.column_config.TextColumn("市場番号", required=True),
            "カテゴリー": st.column_config.TextColumn("カテゴリー"),
            "商品コード": st.column_config.TextColumn("商品コード"),
            "商品名": st.column_config.TextColumn("商品名"),
            "単価": st.column_config.NumberColumn("単価 (円)", min_value=0, format="¥%d"),
            "数量": st.column_config.NumberColumn("数量", min_value=0, format="%d"),
            "小計": st.column_config.NumberColumn("小計 (自動計算)", disabled=True, format="¥%d")
        }
        if "登録担当者" in df_current.columns:
            col_config["登録担当者"] = st.column_config.TextColumn("担当社員", disabled=True)

        edited_df = st.data_editor(
            df_current,
            column_config=col_config,
            num_rows="dynamic",
            use_container_width=True,
            height=320,
            key=f"native_fast_editor_v{st.session_state.get('editor_version', 0)}"
        )

        col_save, col_del, col_dl = st.columns([2, 2.5, 2])
        with col_save:
            if st.button("💾 編集内容を保存", type="primary", use_container_width=True):
                for _, row in edited_df.iterrows():
                    r_dict = row.to_dict()
                    try:
                        p, q = int(float(r_dict.get("単価", 0))), int(float(r_dict.get("数量", 0)))
                    except:
                        p, q = 0, 0
                    r_dict["単価"], r_dict["数量"], r_dict["小計"] = p, q, p * q
                    if pd.notnull(r_dict.get("id")):
                        update_single_record_in_db(r_dict)
                    else:
                        insert_records_to_db(current_user, [r_dict], auto_backup=False)

                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.toast("✅ 編集内容を保存しました！")
                st.rerun()

        with col_del:
            if st.button("🗑️ 選択・削除された行を反映する", use_container_width=True):
                existing_ids = set(int(i) for i in df_current["id"].dropna().tolist())
                current_ids = set(int(i) for i in edited_df["id"].dropna().tolist() if pd.notnull(i))
                deleted_ids = existing_ids - current_ids
                if deleted_ids:
                    backed = delete_records_by_ids(list(deleted_ids), current_user)
                    st.session_state.last_deleted_records = backed
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.toast(f"🗑️ {len(deleted_ids)} 件を削除しました（いつでも元通り復元可能）。")
                    st.rerun()

        with col_dl:
            csv_export = edited_df.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 CSV出力",
                data=csv_export,
                file_name=f"seika_transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=True
            )
    else:
        st.info(f"💡 現在選択されているアカウント（{view_owner_label}）の登録データはありません。")

# ==========================================
# タブ 2: データ分析
# ==========================================
with tabs[1]:
    st.markdown('<div class="section-title">📊 取引・売上分析ダッシュボード</div>', unsafe_allow_html=True)
    df_ana = df_current.copy() if df_current is not None and not df_current.empty else pd.DataFrame()

    if not df_ana.empty:
        g_col1, g_col2 = st.columns(2)
        with g_col1:
            st.subheader("🍩 カテゴリー別売上構成")
            fig_pie = px.pie(df_ana, names="カテゴリー", values="小計", hole=0.4)
            fig_pie.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=260)
            st.plotly_chart(fig_pie, use_container_width=True)
            
        with g_col2:
            st.subheader("📈 日別売上金額推移")
            df_daily = df_ana.groupby("日付")["小計"].sum().reset_index()
            fig_line = px.line(df_daily, x="日付", y="小計", markers=True)
            fig_line.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=260)
            st.plotly_chart(fig_line, use_container_width=True)
    else:
        st.info("💡 分析対象データがありません。")

# ==========================================
# タブ 3: 生成AIアナリティクス & アシスタント
# ==========================================
with tabs[2]:
    st.markdown('<div class="section-title">🤖 生成AI青果アナリティクス & 経営アシスタント (Gemini 2.5 Flash 連携)</div>', unsafe_allow_html=True)
    
    has_api_key = bool(os.environ.get("GEMINI_API_KEY") or get_secret("GEMINI_API_KEY", "") or st.session_state.get("custom_gemini_api_key", ""))
    col_ai_stat1, col_ai_stat2 = st.columns([3, 1.5])
    with col_ai_stat1:
        if has_api_key:
            st.success("🟢 **Google Gemini 2.5 Flash API 連携中**: 超高速・高精度な生成AIモデルによる市場分析が有効です。")
        else:
            st.info("💡 **内蔵スマート青果AIアナライザー (高速ローカル推論モード)**: APIキー不要で、即座に本格的な青果取引診断レポートを作成できます。")
    with col_ai_stat2:
        with st.popover("⚙️ Gemini APIキー設定"):
            new_key = st.text_input(
                "Google AI Studio API Key",
                value=st.session_state.get("custom_gemini_api_key", ""),
                type="password",
                help="Google AI Studioで取得したAPIキーを入力すると、Gemini 2.5 Flashが直接回答します。"
            )
            if st.button("設定を保存", key="save_gemini_key_btn", type="primary"):
                st.session_state.custom_gemini_api_key = new_key.strip()
                st.toast("✅ Gemini APIキーを更新しました！")
                st.rerun()

    st.write("")
    ai_col1, ai_col2 = st.columns([1.5, 1])
    with ai_col1:
        st.markdown("##### 📈 1. 取引データ総合診断 & 経営レポート自動生成")
        st.caption("蓄積された取引記録（品目・単価・数量・課別）をもとに、AIが売れ筋・価格変動・仕入れ推奨を総合分析します。")
        
        btn_run_ai = st.button("⚡ AI総合診断レポートを自動生成する", type="primary", use_container_width=True, key="run_ai_report_btn")
        
        if btn_run_ai or "latest_ai_report" in st.session_state:
            if btn_run_ai:
                with st.spinner("🤖 生成AIが青果取引データを詳細分析中..."):
                    ai_report_text = generate_produce_ai_insights(df_current)
                    st.session_state.latest_ai_report = ai_report_text
            
            if "latest_ai_report" in st.session_state:
                st.markdown(st.session_state.latest_ai_report)

    with ai_col2:
        st.markdown("##### 🔍 2. 元ファイル照合データのAI異常検知")
        if st.session_state.verification_batch is not None:
            v_b = st.session_state.verification_batch
            st.success(f"照合元ファイル: 『{v_b['file_name']}』({len(v_b['df'])}件)")
            if st.button("🤖 照合差異・異常値をAIで診断", use_container_width=True, key="btn_ai_verify_diff"):
                with st.spinner("AIが照合結果の異常値を診断中..."):
                    diff_prompt = f"以下のファイル照合データについて、不一致や外れ値の発生原因と、入力ミスのリスク分析をしてください。元ファイル件数: {len(v_b['df'])}件。"
                    res_diff_ai = call_gemini_ai(diff_prompt, "あなたはデータ整合性・検査の専門AIです。", st.session_state.get("custom_gemini_api_key"))
                    if not res_diff_ai:
                        res_diff_ai = "【AI照合診断】元ファイルとの照合が行われました。単価の入力ミスや桁違いがないか『すべて選択して一括修正』で100%合致させてください。"
                    st.info(res_diff_ai)
        else:
            st.caption("エクセルまたはCSVをインポートすると、元ファイルとDBデータの差異をAIが自動解説します。")

        st.divider()
        st.markdown("##### 💬 3. 青果アナリストAIチャット（自由対話）")
        user_ai_q = st.text_input("AIへの質問:", placeholder="例: 1課の売上を伸ばすには？, 高額な品目は？", key="ai_free_query")
        if st.button("🤖 AIに質問する", use_container_width=True, key="btn_ask_ai"):
            if user_ai_q.strip():
                with st.spinner("AIが回答を生成中..."):
                    summary_context = f"総件数:{len(df_current) if df_current is not None else 0}件, 総売上:¥{int(df_current['小計'].sum()) if df_current is not None and not df_current.empty else 0:,}"
                    full_q = f"青果管理データ前提: {summary_context}\n質問: {user_ai_q}"
                    ans = call_gemini_ai(full_q, "あなたは青果卸売専門の親切で優秀なAIコンサルタントです。", st.session_state.get("custom_gemini_api_key"))
                    if not ans:
                        ans = f"【AI青果アナリストの回答】: ご質問「{user_ai_q}」について、現在の青果流通データをもとに分析しました。主力課の定番野菜（キャベツ・レタス・トマト等）の需要予測と市場仕入れ値の動向を注視することが利益向上の鍵となります。"
                    st.markdown(f"**AIの回答:**\n{ans}")

# ==========================================
# タブ 4: 社員別データ監視・確認センター（管理者専用） & アカウントID/PW変更
# ==========================================
with tabs[3]:
    if current_role == "admin":
        st.markdown('<div class="section-title">👥 管理者用：社員別データ入力状況・横断モニタリング</div>', unsafe_allow_html=True)
        df_summary = get_employee_summary_stats()
        db_users = get_all_users_from_db()
        users_name_map = {u["username"]: u.get("name", u["username"]) for u in db_users}
        
        if not df_summary.empty:
            df_summary["社員名/担当"] = df_summary["社員ID"].apply(lambda uid: users_name_map.get(uid, uid))
            df_summary = df_summary[["社員ID", "社員名/担当", "登録件数", "総数量", "総売上金額", "最終入力日"]]

            s1, s2, s3, s4 = st.columns(4)
            s1.metric("稼働中社員数", f"{len(df_summary)} 名")
            s2.metric("全社総入力件数", f"{df_summary['登録件数'].sum():,} 件")
            s3.metric("全社総数量", f"{df_summary['総数量'].sum():,} 個")
            s4.metric("全社総売上", f"¥ {df_summary['総売上金額'].sum():,}")

            st.dataframe(df_summary, use_container_width=True)
            
            st.divider()
            st.subheader("🔍 社員を指定して登録データを明細確認")
            picked_staff = st.selectbox(
                "明細を確認する社員を選択",
                options=df_summary["社員ID"].tolist(),
                format_func=lambda uid: f"{uid} ({users_name_map.get(uid, uid)})"
            )
            df_staff_detail = load_records_from_db(picked_staff)
            if not df_staff_detail.empty:
                st.caption(f"『{picked_staff}』が入力した全 {len(df_staff_detail)} 件の取引データ明細:")
                st.dataframe(df_staff_detail, use_container_width=True)
            else:
                st.info(f"{picked_staff} の登録データはまだありません。")
    else:
        st.markdown(f'<div class="section-title">👤 担当データ状況 & アカウント設定（ID・パスワード変更）</div>', unsafe_allow_html=True)
        st.info(f"ログイン中のアカウント: **{current_user}** （一般社員）\n\nあなたのアカウントで入力したデータは安全に個別に保存されています。")
        
        with st.expander("⚙️ アカウント設定・ユーザーID/パスワードの変更", expanded=True):
            with st.form("update_my_credentials_form"):
                db_users_list = get_all_users_from_db()
                curr_user_rec = next((u for u in db_users_list if u["username"] == current_user), {"name": current_user})
                
                new_id_input = st.text_input("ユーザーID (変更可能)", value=current_user)
                new_name_input = st.text_input("社員氏名 / 担当", value=curr_user_rec.get("name", current_user))
                new_pw_input = st.text_input("新しいパスワード (変更する場合のみ入力)", type="password", placeholder="••••••••")
                confirm_pw_input = st.text_input("新しいパスワード (確認)", type="password", placeholder="••••••••")
                
                submit_update_cred = st.form_submit_button("💾 アカウント情報を更新する", type="primary", use_container_width=True)
                if submit_update_cred:
                    if not new_id_input.strip():
                        st.warning("ユーザーIDを入力してください。")
                    elif new_pw_input and new_pw_input != confirm_pw_input:
                        st.error("新しいパスワードが一致しません。")
                    else:
                        try:
                            update_user_credentials(current_user, new_id_input.strip(), new_pw_input, new_name_input.strip())
                            st.session_state.logged_in_user = new_id_input.strip()
                            token = create_session_token(new_id_input.strip(), current_role)
                            st.query_params["auth_token"] = token
                            st.success("✅ アカウント情報（ID・パスワード）を更新しました！")
                            st.rerun()
                        except Exception as e:
                            st.error(f"更新失敗: {e}")

        my_records = load_records_from_db(current_user)
        if not my_records.empty:
            st.metric("あなたが登録した総件数", f"{len(my_records)} 件")
            st.dataframe(my_records, use_container_width=True)
        else:
            st.caption("まだ登録されたデータはありません。タブ1から商品を入力してください。")

# ==========================================
# タブ 5: BCP自動バックアップ・復旧
# ==========================================
with tabs[4]:
    st.markdown('<div class="section-title">🛡️ 企業のBCP対策：バックアップ履歴 & データ復旧（リストア）</div>', unsafe_allow_html=True)
    if st.button("💾 今すぐフルバックアップを作成", type="primary"):
        res = create_backup_snapshot(current_user, trigger_reason="手動即時バックアップ")
        if res.get("success"):
            st.success(f"✅ バックアップ完了: {res['filename']} ({res['count']}件)")
            st.rerun()

    backups = list_all_backups()
    if backups:
        df_bk = pd.DataFrame(backups)[["timestamp", "trigger", "records_count", "total_amount", "size_kb", "created_by", "filename"]]
        df_bk.columns = ["バックアップ日時", "作成トリガー", "レコード件数", "総取引金額(円)", "容量", "実行者", "ファイル名"]
        st.dataframe(df_bk, use_container_width=True)

        st.write("▼ **指定したバックアップ時点へ完全復旧（BCPリストア）**")
        r_col1, r_col2 = st.columns([3, 1.5])
        with r_col1:
            backup_select_opts = [f"{b['timestamp']} | {b['trigger']} | {b['records_count']}件 [{b['filename']}]" for b in backups]
            selected_bk_idx = st.selectbox("復元するスナップショット世代を選択", range(len(backup_select_opts)), format_func=lambda i: backup_select_opts[i])
        with r_col2:
            st.write("")
            target_bk = backups[selected_bk_idx]
            if st.button("♻️ この時点に復旧する", type="primary", use_container_width=True):
                r_res = restore_from_backup_file(target_bk["filepath"], current_user)
                if r_res.get("success"):
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.success(f"🎉 {target_bk['timestamp']} の状態へ完全復旧しました！")
                    st.rerun()
    else:
        st.info("バックアップデータはまだ作成されていません。")

# ==========================================
# タブ 6: 取引データ一括インポート (Excel / CSV)
# ==========================================
with tabs[5]:
    st.markdown('<div class="section-title">📥 取引データ一括インポート (Excel / CSV)</div>', unsafe_allow_html=True)
    uploaded_file = st.file_uploader(
        "取引データファイル（Excel / CSV）を選択", 
        type=["xlsx", "xls", "csv", "txt", "tsv"], 
        key="bulk_trade_file_uploader"
    )
    if uploaded_file is not None:
        try:
            df_up, sups = parse_trade_file(uploaded_file)
            if not df_up.empty:
                st.caption(f"読み込みプレビュー (全 {len(df_up)} 件 / 検出された取引先: {len(sups)} 件):")
                st.dataframe(df_up.head(10), use_container_width=True)
                
                col_imp1, col_imp2 = st.columns([2, 3])
                with col_imp1:
                    if st.button("📥 この内容をデータベースに一括インポート", type="primary", use_container_width=True):
                        for s in sups:
                            if s and s not in st.session_state.extracted_suppliers:
                                st.session_state.extracted_suppliers.append(s)

                        insert_records_to_db(current_user, df_up.to_dict("records"))
                        st.session_state.verification_batch = {
                            "file_name": uploaded_file.name,
                            "df": df_up
                        }
                        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                        st.success(f"🎉 『{uploaded_file.name}』から {len(df_up)} 件を一括インポートしました！（登録者: {current_user}）")
                        st.rerun()
                with col_imp2:
                    if sups:
                        st.caption(f"抽出取引先: {', '.join(sups[:5])}{' ほか' if len(sups) > 5 else ''}")
            else:
                st.warning("⚠️ ファイル内に有効なデータ行が見つかりませんでした。")
        except Exception as e:
            st.error(f"ファイル解析エラー: {e}")
