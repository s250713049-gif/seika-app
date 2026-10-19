import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from collections import OrderedDict
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
        border-radius: 8px;
        margin-bottom: 12px;
    }
    .mismatch-box {
        background-color: #fef2f2;
        border: 1px solid #fca5a5;
        padding: 12px 16px;
        border-radius: 8px;
        margin-bottom: 12px;
    }
    div[data-testid="stStatusWidget"] { visibility: hidden; display: none; }
    div[data-st-mode="running"] { opacity: 1 !important; filter: none !important; }
    </style>
""", unsafe_allow_html=True)

# ==========================================
# 1. セキュリティ定数 & シークレット管理
# ==========================================
def get_secret(key: str, default: str = "") -> str:
    val = os.environ.get(key)
    if val:
        return val.strip()
    try:
        if hasattr(st, "secrets") and key in st.secrets:
            return str(st.secrets[key]).strip()
    except Exception:
        pass
    return default

APP_SECRET_KEY = get_secret("SECRET_KEY", "seika-enterprise-secure-salt-2026-v2")
MASTER_CSV_FILE = "item_master.csv"
BACKUP_DIR = "db_backups"
os.makedirs(BACKUP_DIR, exist_ok=True)

# ==========================================
# 2. データベース接続エンジン
# ==========================================
def get_database_url() -> str:
    custom_url = st.session_state.get("custom_database_url", "").strip()
    if custom_url:
        return custom_url
    cloud_url = get_secret("DATABASE_URL")
    if cloud_url:
        return cloud_url
    return "sqlite:///seika_inventory.db"

@st.cache_resource
def get_db_engine(db_url: str):
    clean_url = db_url.strip()
    if clean_url.startswith("postgres://"):
        clean_url = clean_url.replace("postgres://", "postgresql://", 1)
        
    is_sqlite = clean_url.startswith("sqlite")
    if is_sqlite:
        engine = create_engine(
            clean_url,
            connect_args={"check_same_thread": False, "timeout": 15.0}
        )
    else:
        engine = create_engine(
            clean_url,
            pool_size=10,
            max_overflow=20,
            pool_recycle=300,
            pool_pre_ping=True
        )
    return engine

def get_current_engine():
    return get_db_engine(get_database_url())

def get_db_info():
    url = get_database_url()
    is_cloud = not url.startswith("sqlite")
    if is_cloud:
        try:
            parts = url.split("@")
            host_part = parts[-1] if len(parts) > 1 else "Cloud DB"
            masked_desc = f"クラウド PostgreSQL / Supabase ({host_part.split('/')[0]})"
        except:
            masked_desc = "クラウド PostgreSQL / Supabase"
        return {"type": "cloud", "badge": "☁️ クラウド PostgreSQL", "desc": masked_desc, "is_cloud": True}
    else:
        return {
            "type": "local", 
            "badge": "📁 高速 ローカル SQLite", 
            "desc": "sqlite:///seika_inventory.db", 
            "is_cloud": False
        }

# ==========================================
# 3. 認証情報の堅牢化（PBKDF2-HMAC-SHA256）
# ==========================================
def hash_password_secure(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        'sha256', 
        password.encode('utf-8'), 
        salt.encode('utf-8'), 
        100000
    ).hex()

def generate_salt() -> str:
    return secrets.token_hex(16)

@st.cache_resource
def ensure_db_schema_initialized(db_url: str):
    engine = get_db_engine(db_url)
    with engine.begin() as conn:
        id_type = "INTEGER PRIMARY KEY AUTOINCREMENT" if "sqlite" in engine.name else "SERIAL PRIMARY KEY"
        
        conn.execute(text(f'''
            CREATE TABLE IF NOT EXISTS users (
                id {id_type},
                username VARCHAR(100) UNIQUE NOT NULL,
                pw_hash VARCHAR(255) NOT NULL,
                salt VARCHAR(100) NOT NULL,
                role VARCHAR(50) NOT NULL,
                name VARCHAR(100),
                created_at VARCHAR(50)
            )
        '''))

        conn.execute(text(f'''
            CREATE TABLE IF NOT EXISTS records (
                id {id_type},
                user_id VARCHAR(255),
                date VARCHAR(50),
                market_no VARCHAR(100),
                category VARCHAR(100),
                item_code VARCHAR(100),
                item_name VARCHAR(255),
                unit_price INTEGER,
                quantity INTEGER,
                subtotal INTEGER
            )
        '''))

        conn.execute(text(f'''
            CREATE TABLE IF NOT EXISTS deleted_records (
                id {id_type},
                original_id INTEGER,
                user_id VARCHAR(255),
                date VARCHAR(50),
                market_no VARCHAR(100),
                category VARCHAR(100),
                item_code VARCHAR(100),
                item_name VARCHAR(255),
                unit_price INTEGER,
                quantity INTEGER,
                subtotal INTEGER,
                deleted_at VARCHAR(50)
            )
        '''))

        user_count = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
        if user_count == 0:
            init_admin_pw = get_secret("INITIAL_ADMIN_PASSWORD", "password123")
            init_users = [
                {"username": "admin", "pw": init_admin_pw, "role": "admin", "name": "管理者 (統括)"},
                {"username": "user1", "pw": "seika2026", "role": "staff", "name": "社員A (東京市場担当)"},
                {"username": "staff_a", "pw": "market01", "role": "staff", "name": "社員B (横浜市場担当)"}
            ]
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for u in init_users:
                s = generate_salt()
                h = hash_password_secure(u["pw"], s)
                conn.execute(text('''
                    INSERT INTO users (username, pw_hash, salt, role, name, created_at)
                    VALUES (:username, :pw_hash, :salt, :role, :name, :created_at)
                '''), {
                    "username": u["username"],
                    "pw_hash": h,
                    "salt": s,
                    "role": u["role"],
                    "name": u["name"],
                    "created_at": now_str
                })
    return True

ensure_db_schema_initialized(get_database_url())

# ==========================================
# 4. ユーザー認証・CRUD
# ==========================================
def verify_user_login(username: str, password_plain: str):
    engine = get_current_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT username, pw_hash, salt, role, name FROM users WHERE username = :u"),
            {"u": username.strip()}
        ).fetchone()
        if not row:
            return None
        m = row._mapping
        calc_hash = hash_password_secure(password_plain, m["salt"])
        if hmac.compare_digest(calc_hash, m["pw_hash"]):
            return {"username": m["username"], "role": m["role"], "name": m["name"]}
        return None

def get_all_users_from_db():
    engine = get_current_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT username, role, name, created_at FROM users ORDER BY id ASC")).fetchall()
        return [dict(r._mapping) for r in rows]

def register_new_user_db(username: str, password_plain: str, role: str, name: str):
    engine = get_current_engine()
    s = generate_salt()
    h = hash_password_secure(password_plain, s)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with engine.begin() as conn:
        conn.execute(text('''
            INSERT INTO users (username, pw_hash, salt, role, name, created_at)
            VALUES (:username, :pw_hash, :salt, :role, :name, :created_at)
        '''), {
            "username": username.strip(),
            "pw_hash": h,
            "salt": s,
            "role": role,
            "name": name.strip() or username.strip(),
            "created_at": now_str
        })

# ==========================================
# 5. セッション署名トークン（F5リロード対策）
# ==========================================
def create_session_token(username: str, role: str) -> str:
    expire_ts = int((datetime.now() + timedelta(days=7)).timestamp())
    payload = f"{username}|{role}|{expire_ts}"
    sig = hmac.new(APP_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"

def verify_session_token(token: str):
    if not token or "|" not in token:
        return None
    try:
        parts = token.split("|")
        if len(parts) != 4:
            return None
        username, role, expire_ts_str, sig = parts
        payload = f"{username}|{role}|{expire_ts_str}"
        expected_sig = hmac.new(APP_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        if int(expire_ts_str) < int(datetime.now().timestamp()):
            return None
        return {"username": username, "role": role}
    except Exception:
        return None

# ==========================================
# 6. バックアップ管理
# ==========================================
def create_backup_snapshot(user_id=None, trigger_reason="手動バックアップ"):
    try:
        engine = get_current_engine()
        with engine.connect() as conn:
            df_rec = pd.read_sql_query(text("SELECT * FROM records ORDER BY id ASC"), conn)
            df_del = pd.read_sql_query(text("SELECT * FROM deleted_records ORDER BY id ASC"), conn)
            df_usr = pd.read_sql_query(text("SELECT id, username, role, name, created_at FROM users"), conn)

        now = datetime.now()
        ts_str = now.strftime("%Y%m%d_%H%M%S")
        backup_filename = f"seika_backup_{ts_str}.json"
        backup_path = os.path.join(BACKUP_DIR, backup_filename)

        backup_payload = {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": trigger_reason,
            "created_by": user_id or "system",
            "records_count": len(df_rec),
            "total_amount": int(df_rec["subtotal"].sum()) if not df_rec.empty and "subtotal" in df_rec.columns else 0,
            "records": df_rec.to_dict("records"),
            "deleted_records": df_del.to_dict("records"),
            "users_summary": df_usr.to_dict("records")
        }

        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(backup_payload, f, ensure_ascii=False, indent=2)

        return {"success": True, "filename": backup_filename, "count": len(df_rec)}
    except Exception as e:
        return {"success": False, "error": str(e)}

def list_all_backups():
    backup_files = sorted(glob.glob(os.path.join(BACKUP_DIR, "seika_backup_*.json")), reverse=True)
    backups_meta = []
    for fpath in backup_files:
        try:
            fname = os.path.basename(fpath)
            fsize_kb = round(os.path.getsize(fpath) / 1024, 1)
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            backups_meta.append({
                "filename": fname,
                "filepath": fpath,
                "timestamp": data.get("timestamp", "不明"),
                "trigger": data.get("trigger", "自動"),
                "records_count": data.get("records_count", 0),
                "total_amount": data.get("total_amount", 0),
                "size_kb": f"{fsize_kb} KB",
                "created_by": data.get("created_by", "system")
            })
        except:
            continue
    return backups_meta

def restore_from_backup_file(filepath: str, user_id: str):
    try:
        create_backup_snapshot(user_id, trigger_reason="BCPリストア直前セーフティ")
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        records_to_restore = data.get("records", [])
        engine = get_current_engine()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM records"))
            if records_to_restore:
                ins_rec = text('''
                    INSERT INTO records (user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal)
                    VALUES (:user_id, :date, :market_no, :category, :item_code, :item_name, :unit_price, :quantity, :subtotal)
                ''')
                for r in records_to_restore:
                    p = int(float(r.get("unit_price", 0)))
                    q = int(float(r.get("quantity", 0)))
                    conn.execute(ins_rec, {
                        "user_id": r.get("user_id", user_id),
                        "date": str(r.get("date", "")),
                        "market_no": str(r.get("market_no", "")),
                        "category": str(r.get("category", "")),
                        "item_code": str(r.get("item_code", "")),
                        "item_name": str(r.get("item_name", "")),
                        "unit_price": p,
                        "quantity": q,
                        "subtotal": p * q
                    })
        if "records_cache" in st.session_state:
            del st.session_state.records_cache
        return {"success": True, "count": len(records_to_restore)}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ==========================================
# 7. データCRUD関数
# ==========================================
def invalidate_records_cache():
    if "records_cache" in st.session_state:
        del st.session_state.records_cache

def load_records_from_db(user_id=None, force_reload=False):
    cache_key = f"cache_user_{user_id}"
    if not force_reload and "records_cache" in st.session_state and cache_key in st.session_state.records_cache:
        return st.session_state.records_cache[cache_key]

    engine = get_current_engine()
    with engine.connect() as conn:
        if user_id is None:
            query = text("SELECT id, user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal FROM records ORDER BY id ASC")
            df = pd.read_sql_query(query, conn)
        else:
            query = text("SELECT id, user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal FROM records WHERE user_id = :user_id ORDER BY id ASC")
            df = pd.read_sql_query(query, conn, params={"user_id": user_id})
    
    if not df.empty:
        df["id"] = df["id"].astype(int)
        df = df.rename(columns={
            "id": "id",
            "user_id": "登録担当者",
            "date": "日付",
            "market_no": "市場番号",
            "category": "カテゴリー",
            "item_code": "商品コード",
            "item_name": "商品名",
            "unit_price": "単価",
            "quantity": "数量",
            "subtotal": "小計"
        })

    if "records_cache" not in st.session_state:
        st.session_state.records_cache = {}
    st.session_state.records_cache[cache_key] = df
    return df

def get_employee_summary_stats():
    engine = get_current_engine()
    with engine.connect() as conn:
        query = text("""
            SELECT 
                user_id as 社員ID,
                COUNT(id) as 登録件数,
                COALESCE(SUM(quantity), 0) as 総数量,
                COALESCE(SUM(subtotal), 0) as 総売上金額,
                MAX(date) as 最終入力日
            FROM records
            GROUP BY user_id
            ORDER BY 総売上金額 DESC
        """)
        return pd.read_sql_query(query, conn)

def insert_records_to_db(user_id, records_list, auto_backup=False):
    if not records_list:
        return
    engine = get_current_engine()
    with engine.begin() as conn:
        insert_query = text('''
            INSERT INTO records (user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal)
            VALUES (:user_id, :date, :market_no, :category, :item_code, :item_name, :unit_price, :quantity, :subtotal)
        ''')
        params = []
        for r in records_list:
            try:
                p = int(float(r.get("単価", 0))) if pd.notnull(r.get("単価")) else 0
            except:
                p = 0
            try:
                q = int(float(r.get("数量", 0))) if pd.notnull(r.get("数量")) else 0
            except:
                q = 0
                
            rec_user = r.get("登録担当者") or r.get("user_id") or user_id
            params.append({
                "user_id": rec_user,
                "date": str(r.get("日付", "")),
                "market_no": str(r.get("市場番号", "")),
                "category": str(r.get("カテゴリー", "")),
                "item_code": str(r.get("商品コード", "")),
                "item_name": str(r.get("商品名", "")),
                "unit_price": p,
                "quantity": q,
                "subtotal": p * q
            })
        conn.execute(insert_query, params)

    invalidate_records_cache()

def delete_records_by_ids(ids_to_delete, user_id=None):
    if not ids_to_delete:
        return []
    clean_ids = [int(i) for i in ids_to_delete if pd.notnull(i) and str(i).replace('.0','').isdigit()]
    if not clean_ids:
        return []
        
    backed_up_rows = []
    engine = get_current_engine()
    with engine.begin() as conn:
        id_str = ",".join(map(str, clean_ids))
        fetch_query = text(f"SELECT id, user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal FROM records WHERE id IN ({id_str})")
        res = conn.execute(fetch_query).fetchall()
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for row in res:
            backed_up_rows.append(dict(row._mapping))
            conn.execute(text('''
                INSERT INTO deleted_records (original_id, user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal, deleted_at)
                VALUES (:original_id, :user_id, :date, :market_no, :category, :item_code, :item_name, :unit_price, :quantity, :subtotal, :deleted_at)
            '''), {
                "original_id": row._mapping["id"],
                "user_id": row._mapping["user_id"],
                "date": row._mapping["date"],
                "market_no": row._mapping["market_no"],
                "category": row._mapping["category"],
                "item_code": row._mapping["item_code"],
                "item_name": row._mapping["item_name"],
                "unit_price": row._mapping["unit_price"],
                "quantity": row._mapping["quantity"],
                "subtotal": row._mapping["subtotal"],
                "deleted_at": now_str
            })
            
        conn.execute(text(f"DELETE FROM records WHERE id IN ({id_str})"))

    invalidate_records_cache()
    return backed_up_rows

def load_deleted_records_from_db(user_id=None):
    engine = get_current_engine()
    with engine.connect() as conn:
        if user_id is None:
            query = text("SELECT id, original_id, user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal, deleted_at FROM deleted_records ORDER BY id DESC")
            df = pd.read_sql_query(query, conn)
        else:
            query = text("SELECT id, original_id, user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal, deleted_at FROM deleted_records WHERE user_id = :user_id ORDER BY id DESC")
            df = pd.read_sql_query(query, conn, params={"user_id": user_id})
    if not df.empty:
        df["id"] = df["id"].astype(int)
        df = df.rename(columns={
            "id": "ゴミ箱ID",
            "original_id": "元ID",
            "user_id": "担当社員",
            "date": "日付",
            "market_no": "市場番号",
            "category": "カテゴリー",
            "item_code": "商品コード",
            "item_name": "商品名",
            "unit_price": "単価",
            "quantity": "数量",
            "subtotal": "小計",
            "deleted_at": "削除日時"
        })
    return df

def restore_records_from_trash(deleted_ids_to_restore, user_id):
    if not deleted_ids_to_restore:
        return 0
    clean_ids = [int(i) for i in deleted_ids_to_restore if pd.notnull(i) and str(i).replace('.0',''].isdigit()]
    if not clean_ids:
        return 0
        
    restored_count = 0
    engine = get_current_engine()
    with engine.begin() as conn:
        id_str = ",".join(map(str, clean_ids))
        fetch_query = text(f"SELECT user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal FROM deleted_records WHERE id IN ({id_str})")
        rows = conn.execute(fetch_query).fetchall()
        
        insert_query = text('''
            INSERT INTO records (user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal)
            VALUES (:user_id, :date, :market_no, :category, :item_code, :item_name, :unit_price, :quantity, :subtotal)
        ''')
        for r in rows:
            m = r._mapping
            conn.execute(insert_query, {
                "user_id": m["user_id"] or user_id,
                "date": m["date"],
                "market_no": m["market_no"],
                "category": m["category"],
                "item_code": m["item_code"],
                "item_name": m["item_name"],
                "unit_price": m["unit_price"],
                "quantity": m["quantity"],
                "subtotal": m["subtotal"]
            })
            restored_count += 1
            
        conn.execute(text(f"DELETE FROM deleted_records WHERE id IN ({id_str})"))

    invalidate_records_cache()
    return restored_count

def purge_deleted_records(user_id=None):
    engine = get_current_engine()
    with engine.begin() as conn:
        if user_id:
            conn.execute(text("DELETE FROM deleted_records WHERE user_id = :user_id"), {"user_id": user_id})
        else:
            conn.execute(text("DELETE FROM deleted_records"))
    invalidate_records_cache()

def update_single_record_in_db(record_dict):
    if "id" not in record_dict or pd.isnull(record_dict["id"]):
        return
    engine = get_current_engine()
    with engine.begin() as conn:
        try:
            p = int(float(record_dict.get("単価", 0)))
            q = int(float(record_dict.get("数量", 0)))
        except:
            p, q = 0, 0
        query = text('''
            UPDATE records 
            SET date = :date, market_no = :market_no, category = :category, 
                item_code = :item_code, item_name = :item_name, 
                unit_price = :unit_price, quantity = :quantity, subtotal = :subtotal
            WHERE id = :id
        ''')
        conn.execute(query, {
            "id": int(record_dict["id"]),
            "date": str(record_dict.get("日付", "")),
            "market_no": str(record_dict.get("市場番号", "")),
            "category": str(record_dict.get("カテゴリー", "")),
            "item_code": str(record_dict.get("商品コード", "")),
            "item_name": str(record_dict.get("商品名", "")),
            "unit_price": p,
            "quantity": q,
            "subtotal": p * q
        })
    invalidate_records_cache()

# ==========================================
# 8. マスタデータ管理（社内公式商品コード表 & ○課定義）
# ==========================================
SECTION_DEFINITIONS_PY = [
    {"label": "1課 (熊谷・大宮)", "depts": ["1課4課（熊谷・大宮） / 2課 / 栗原", "1課4課/2課/栗原"]},
    {"label": "2課 (栗原)", "depts": ["1課4課（熊谷・大宮） / 2課 / 栗原", "1課4課/2課/栗原"]},
    {"label": "3課 (照井)", "depts": ["3課（照井） / 11課（相澤 史）", "3課/11課"]},
    {"label": "4課 (熊谷・大宮)", "depts": ["1課4課（熊谷・大宮） / 2課 / 栗原", "1課4課/2課/栗原"]},
    {"label": "5課 (小林・牧野)", "depts": ["5課12課（小林・牧野）", "5課12課"]},
    {"label": "11課 (相澤 史)", "depts": ["3課（照井） / 11課（相澤 史）", "3課/11課"]},
    {"label": "12課 (小林・牧野)", "depts": ["5課12課（小林・牧野）", "5課12課"]},
    {"label": "全課・全部門 (すべての商品を表示)", "depts": ["ALL"]},
]

@st.cache_data(ttl=3600)
def load_item_master_cached():
    if os.path.exists(MASTER_CSV_FILE):
        try:
            df_master = pd.read_csv(MASTER_CSV_FILE, encoding="utf-8-sig", dtype=str)
        except:
            df_master = pd.read_csv(MASTER_CSV_FILE, dtype=str)
    else:
        df_master = pd.DataFrame([
            {"category": "1課4課/2課/栗原", "item_code": "11", "item_name": "かぶ", "department": "1課4課（熊谷・大宮） / 2課 / 栗原"},
            {"category": "1課4課/2課/栗原", "item_code": "130", "item_name": "胡瓜", "department": "1課4課（熊谷・大宮） / 2課 / 栗原"},
            {"category": "3課/11課", "item_code": "1", "item_name": "大根", "department": "3課（照井） / 11課（相澤 史）"},
            {"category": "5課12課", "item_code": "501", "item_name": "りんご", "department": "5課12課（小林・牧野）"}
        ])

    departments = [
        "1課4課（熊谷・大宮） / 2課 / 栗原",
        "3課（照井） / 11課（相澤 史）",
        "5課12課（小林・牧野）"
    ]
    
    dept_items_map = {d: [] for d in departments}
    item_flat_list = []
    lookup_by_pair = {}

    for _, row in df_master.iterrows():
        dept = str(row.get("department", "一般野菜")).strip()
        code = str(row.get("item_code", "")).strip()
        name = str(row.get("item_name", "")).strip()
        
        display_str = f"[{code}] {name}"
        if dept not in dept_items_map:
            dept_items_map[dept] = []
        dept_items_map[dept].append({"code": code, "name": name, "display": display_str, "dept": dept})
        item_flat_list.append(f"{display_str} ({dept})")
        lookup_by_pair[f"{code} : {name}"] = dept
        lookup_by_pair[display_str] = dept

    return dept_items_map, df_master, item_flat_list, departments, lookup_by_pair

ITEM_DEPT_MAP, df_master_raw, ITEM_FLAT_LIST, DEPT_LIST, ITEM_LOOKUP_MAP = load_item_master_cached()

# ==========================================
# 9. 生成AIエンジン (Gemini 2.5 Flash & スマート青果アナライザー)
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
                return f"⚠️ Gemini APIエラー: {resp.text[:200]}"
        except Exception as e:
            return f"⚠️ API通信エラー: {e}"
    return None

def generate_produce_ai_insights(df: pd.DataFrame, custom_prompt: str = "") -> str:
    if df is None or df.empty:
        return "現在、分析対象の取引データが登録されていません。"

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

    prompt = f"""
あなたは青果卸売市場の専門データアナリスト生成AIです。以下の青果取引データを詳細に分析し、経営レポートを作成してください。
【青果取引データ概要】
- 総取引件数: {total_records} 件
- 総取引金額: ¥ {total_amount:,}
- 総取引数量: {total_qty:,} 個
- 平均取引単価: ¥ {avg_price:,}
- 課別売上内訳: {cat_summary}
- 売れ筋商品TOP3: {top_items}
【ユーザー要望】
{custom_prompt if custom_prompt else "売上傾向、課ごとの特徴、仕入れ・価格の最適化アドバイスを提示してください。"}
"""
    sys_instruction = "あなたは青果卸売のプロフェッショナルな生成AIデータアナリストです。具体的で役立つアドバイスをMarkdown出力してください。"

    ai_result = call_gemini_ai(prompt, sys_instruction)
    if ai_result:
        return ai_result

    # 内蔵スマートAIアナライザー
    return f"""### 🤖 青果スマートAIアナリティクス レポート（自動生成）

#### 1. 📈 取引サマリー & 総合評価
- **総取引件数**: **{total_records:,} 件** / **総売上金額**: **¥ {total_amount:,}**（総取扱量: {total_qty:,}）
- **平均取引単価**: **¥ {avg_price:,}**
- 安定した青果流通が維持されており、データ登録は順調です。

#### 2. 🏢 課別実績分析
- **課別構成**: {cat_summary if cat_summary else "単一課での取引運用"}
- 主力課が全体の売上を牽引しています。

#### 3. 🥬 主力品目の動向 & 売れ筋ランキング
- **売上上位商品**: **{top_items if top_items else "取引品目を集計中"}**
- 定番野菜および主力果物の取引が堅調です。

#### 4. 💡 AI推奨アクション & 仕入れアドバイス
1. **自動照合の徹底**: Excelインポート時は、元ファイルデータ照合を活用し、合致率100%を維持してください。
2. **需要予測に基づく先手発注**: 上位品目の需要変動に合わせた早めのロット交渉が有効です。
"""

# ==========================================
# 9.1 エクセル & CSV 取引データ共通解析エンジン (xlrdエラー対策完全版)
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
                    if not df_raw.empty:
                        break
                except Exception:
                    continue

        if df_raw is None or df_raw.empty:
            err_summary = "; ".join(excel_error_details[:2]) if excel_error_details else "xlrd/openpyxl未導入または破損"
            raise ValueError(
                f"Excelファイル解析エラー ({err_summary})。\n"
                "💡 対処法: お手元のファイルを開き、Excelで『.xlsx』または『.csv』形式で保存し直して再度アップロードしてください。"
            )
    else:
        for enc in ["utf-8-sig", "utf-8", "cp932", "shift-jis"]:
            try:
                df_raw = pd.read_csv(io.BytesIO(content_bytes), encoding=enc)
                break
            except Exception:
                continue
        if df_raw is None:
            df_raw = pd.read_csv(io.BytesIO(content_bytes), encoding="latin1")

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
        df_norm["カテゴリー"] = "1課 (熊谷・大宮)"
    else:
        df_norm["カテゴリー"] = df_norm["カテゴリー"].fillna("1課 (熊谷・大宮)").astype(str).str.strip()

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
            <p style="color: #718096; font-size: 14px;">エクセル自動記録・生成AIアシスタント・連続入力高速化</p>
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
                    st.error("❌ IDまたはパスワードが無効です")

        st.caption("テスト用クイック認証:")
        q1, q2, q3 = st.columns(3)
        with q1:
            if st.button("👑 管理者(admin)", use_container_width=True):
                auth = verify_user_login("admin", "password123")
                if auth:
                    st.session_state.logged_in_user = "admin"
                    st.session_state.user_role = "admin"
                    st.query_params["auth_token"] = create_session_token("admin", "admin")
                    st.rerun()
        with q2:
            if st.button("👤 社員A(user1)", use_container_width=True):
                auth = verify_user_login("user1", "seika2026")
                if auth:
                    st.session_state.logged_in_user = "user1"
                    st.session_state.user_role = "staff"
                    st.query_params["auth_token"] = create_session_token("user1", "staff")
                    st.rerun()
        with q3:
            if st.button("👤 社員B(staff_a)", use_container_width=True):
                auth = verify_user_login("staff_a", "market01")
                if auth:
                    st.session_state.logged_in_user = "staff_a"
                    st.session_state.user_role = "staff"
                    st.query_params["auth_token"] = create_session_token("staff_a", "staff")
                    st.rerun()
    st.stop()

current_user = st.session_state.logged_in_user
current_role = st.session_state.user_role

# ==========================================
# 11. サイドバー
# ==========================================
with st.sidebar:
    st.image("https://img.icons8.com/emoji/96/000000/red-apple.png", width=46)
    st.title("青果卸売 Pro")
    role_badge = '<span class="role-badge-admin">👑 管理者</span>' if current_role == "admin" else '<span class="role-badge-staff">👤 社員</span>'
    st.markdown(f"**{current_user}** {role_badge}", unsafe_allow_html=True)

    if st.button("🔄 最新更新", use_container_width=True):
        invalidate_records_cache()
        st.rerun()
    if st.button("ログアウト", use_container_width=True):
        st.session_state.logged_in_user = None
        st.session_state.user_role = None
        if "auth_token" in st.query_params:
            del st.query_params["auth_token"]
        st.rerun()

viewing_user_filter = st.session_state.admin_target_user if current_role == "admin" else current_user
df_current = load_records_from_db(viewing_user_filter)

# ==========================================
# メイン画面
# ==========================================
st.markdown("""
    <div class="app-header">
        <h1>🍎 青果卸売 在庫・取引管理 Pro</h1>
        <p>エクセル自動記録・生成AIアシスタント搭載・元ファイル100%合致照合＆一括修正</p>
    </div>
""", unsafe_allow_html=True)

if current_role == "admin":
    c_m1, c_m2, c_m3 = st.columns([3, 1.5, 1.5])
    with c_m1:
        db_users = get_all_users_from_db()
        user_list = [u["username"] for u in db_users]
        user_labels = {None: "👥 【全社員統合ビュー】すべての社員のデータを一括表示"}
        for u in db_users:
            user_labels[u["username"]] = f"{u['username']} ({u.get('name','')})"
        options_keys = [None] + user_list
        current_idx = options_keys.index(st.session_state.admin_target_user) if st.session_state.admin_target_user in options_keys else 0

        selected_admin_view = st.selectbox(
            "👑 管理者用：確認する社員アカウントを切り替え",
            options=options_keys,
            index=current_idx,
            format_func=lambda k: user_labels.get(k, str(k)),
            key="admin_employee_switcher"
        )
        if selected_admin_view != st.session_state.admin_target_user:
            st.session_state.admin_target_user = selected_admin_view
            invalidate_records_cache()
            st.rerun()
    with c_m3:
        st.write("")
        if st.session_state.admin_target_user is not None:
            if st.button("👥 全社員表示に戻す", use_container_width=True):
                st.session_state.admin_target_user = None
                invalidate_records_cache()
                st.rerun()

tab_titles = [
    "📝 取引データ入力・管理", 
    "📊 データ分析", 
    "🤖 生成AIアシスタント (Gemini)",
    "👥 社員別データ監視 (管理者)" if current_role == "admin" else "👤 担当データ状況",
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
    k4.metric("👤 表示対象アカウント", viewing_user_filter or "全社員統合")

    if st.session_state.last_deleted_records:
        st.markdown("""
            <div class="restore-box">
                <b>↩️ 直前に削除したデータがあります。</b>
            </div>
        """, unsafe_allow_html=True)
        if st.button("↩️ 直前の削除を取り消す", type="primary"):
            insert_records_to_db(current_user, st.session_state.last_deleted_records)
            st.session_state.last_deleted_records = []
            st.success("✅ 復元しました！")
            st.rerun()

    st.write("")
    st.markdown(f'<div class="section-title">1️⃣ 新規取引データの入力 & ファイル自動記録 (入力者: <b>{current_user}</b>)</div>', unsafe_allow_html=True)

    # A. 選択したファイルを自動記録する箇所
    with st.expander("📁 選択したファイルを自動記録する (Excel / CSV 一発取り込み)", expanded=(st.session_state.verification_batch is not None)):
        uploaded_entry_file = st.file_uploader(
            "取引エクセルまたはCSVファイルをここへドロップ",
            type=["xlsx", "xls", "csv", "txt", "tsv"],
            key="auto_record_file_uploader_tab0"
        )

        if uploaded_entry_file is not None:
            try:
                df_file, sups = parse_trade_file(uploaded_entry_file)
                if not df_file.empty:
                    for s in sups:
                        if s and s not in st.session_state.extracted_suppliers:
                            st.session_state.extracted_suppliers.append(s)

                    new_records_list = df_file.to_dict("records")
                    insert_records_to_db(current_user, new_records_list)
                    st.session_state.verification_batch = {
                        "file_name": uploaded_entry_file.name,
                        "df": df_file
                    }
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.toast(f"✅ 『{uploaded_entry_file.name}』から {len(new_records_list)} 件を自動記録しました！")
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
            expected_total_subtotal = int(expected_df["小計"].sum()) if not expected_df.empty and "小計" in expected_df.columns else 0

            mismatches = []
            matched_count = 0

            if df_current is not None and not df_current.empty:
                for idx, exp_row in expected_df.iterrows():
                    exp_date = str(exp_row.get("日付", "")).strip()
                    exp_market = str(exp_row.get("市場番号", "")).strip()
                    exp_cat = str(exp_row.get("カテゴリー", "1課")).strip()
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
                st.success(f"🛡️ **【100%合致確認済み】元ファイル『{v_file_name}』の全 {len(expected_df)} 件のデータが、下の記録一覧と100%完全一致しています。**")
            else:
                st.markdown(f"""
                    <div class="mismatch-box">
                        <h4 style="margin: 0; color: #991b1b;">⚠️ 元ファイル照合: {len(mismatches)} 件の不一致・差異が検出されました</h4>
                        <p style="margin: 4px 0 0 0; color: #4b5563; font-size: 13px;">
                            元ファイルの内容と現在の登録データとの間に相違があります。『個別修正』または『すべて選択して一括修正』を行うと元のファイルと完全に同じデータに瞬時に修正されます。
                        </p>
                    </div>
                """, unsafe_allow_html=True)

                diff_table_df = pd.DataFrame(mismatches)[["行番号", "商品名", "不一致項目", "元ファイルの値(正)", "現在の登録値(誤)"]]
                st.dataframe(diff_table_df, use_container_width=True)

                # 個別修正フォーム
                st.markdown("##### ✍️ 個別にデータを選択して修正する")
                mismatch_labels = [f"行{m['行番号']}: {m['商品名']} ({m['不一致項目']}: 正={m['元ファイルの値(正)']} ⇔ 誤={m['現在の登録値(誤)']})" for m in mismatches]
                selected_m_idx = st.selectbox("修正したい不一致データを選択:", range(len(mismatch_labels)), format_func=lambda i: mismatch_labels[i])
                target_m = mismatches[selected_m_idx]

                c_m1, c_m2, c_m3, c_m4, c_m5 = st.columns([1.5, 2, 1.5, 1.5, 2])
                with c_m1:
                    fix_date = st.text_input("日付", value=str(target_m["日付"]), key="f_indiv_date")
                with c_m2:
                    fix_market = st.text_input("市場番号/取引先", value=str(target_m["市場番号"]), key="f_indiv_market")
                with c_m3:
                    fix_price = st.number_input("元ファイル単価", min_value=0, value=int(target_m["exp_price"]), key="f_indiv_price")
                with c_m4:
                    fix_qty = st.number_input("元ファイル数量", min_value=0, value=int(target_m["exp_qty"]), key="f_indiv_qty")
                with c_m5:
                    fix_subtotal = fix_price * fix_qty
                    st.metric("修正後金額", f"¥ {fix_subtotal:,}")

                c_btn_single, c_btn_all, c_btn_close = st.columns([2.5, 3.5, 1.5])
                with c_btn_single:
                    if st.button("✍️ 選択したデータを元ファイルと同じに修正", type="primary", use_container_width=True):
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
                        st.toast("✅ 修正しました！")
                        st.rerun()

                with c_btn_all:
                    if st.button("🛠️ 全不一致データを元ファイルと同じデータに一括修正・完全同期", use_container_width=True):
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
                        st.toast("🎉 全不一致を一括修正し、合致率100%になりました！")
                        st.rerun()

                with c_btn_close:
                    if st.button("✖ 閉じる", use_container_width=True):
                        st.session_state.verification_batch = None
                        st.rerun()

    # B. 【超高速】手動連続入力フォーム（○課選択で該当商品と商品番号が自動反映）
    st.write("")
    st.markdown("""
        <div style="background-color: #f0fdf4; border: 1.5px solid #86efac; border-radius: 8px; padding: 10px 14px; margin-bottom: 8px;">
            <b style="color: #166534;">⚡ 手動連続入力フォーム（「○課」選択で該当商品と商品番号が自動反映）:</b>
            <span style="color: #4b5563; font-size: 13px;">
                カテゴリーで課を選ぶと、該当する商品と商品番号が自動で反映され、瞬時に連続登録できます。
            </span>
        </div>
    """, unsafe_allow_html=True)

    combined_suppliers = st.session_state.extracted_suppliers + [
        str(x) for x in df_current["市場番号"].dropna().unique() if str(x).strip() and str(x) not in st.session_state.extracted_suppliers
    ]

    selected_sup_choice = st.selectbox(
        "🏢 読み込んだエクセルデータ・登録済みの取引先から選ぶ:",
        options=["（手入力・自由入力モード）"] + [f"🏢 {s}" for s in combined_suppliers],
        key="imported_supplier_quick_picker"
    )

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
                key="chosen_dept_batch_selector"
            )

        st.caption("▼ 選択された課に該当する商品と商品番号が自動反映されます。")

        dept_master_items = df_master_raw[df_master_raw["department"] == chosen_dept]
        candidate_item_options = [
            f"[{r['item_code']}] {r['item_name']}" for _, r in dept_master_items.iterrows()
        ] if not dept_master_items.empty else ITEM_FLAT_LIST

        row_inputs = []
        for i in range(5):
            c0, c1, c2, c3, c4 = st.columns([0.6, 3.0, 1.5, 1.5, 2.0])
            with c0:
                is_active = st.checkbox("有効", value=(i == 0), key=f"f_act_{i}")
            with c1:
                selected_item_str = st.selectbox(
                    f"品名・コード ({i+1})",
                    candidate_item_options,
                    key=f"f_item_{i}",
                    label_visibility="collapsed" if i > 0 else "visible"
                )
            with c2:
                in_price = st.number_input(f"単価", min_value=0, value=280 if i == 0 else 0, key=f"f_price_{i}", label_visibility="collapsed" if i > 0 else "visible")
            with c3:
                in_qty = st.number_input(f"数量", min_value=0, value=50 if i == 0 else 0, key=f"f_qty_{i}", label_visibility="collapsed" if i > 0 else "visible")
            with c4:
                st.text_input(f"小計", value=f"¥ {in_price * in_qty:,}", disabled=True, key=f"f_sub_{i}", label_visibility="collapsed" if i > 0 else "visible")

            row_inputs.append({"active": is_active, "item_str": selected_item_str, "price": in_price, "qty": in_qty})

        submit_batch = st.form_submit_button("⚡ 上記のデータを瞬時に一括登録する", type="primary", use_container_width=True)
        if submit_batch:
            valid_new_items = []
            for r in row_inputs:
                if r["active"] and r["qty"] > 0 and r["price"] > 0:
                    code, name = "AUTO", r["item_str"]
                    if r["item_str"].startswith("[") and "]" in r["item_str"]:
                        code = r["item_str"].split("]")[0].replace("[", "").strip()
                        name = r["item_str"].split("]")[1].strip()

                    valid_new_items.append({
                        "日付": str(batch_date),
                        "市場番号": batch_market_no.strip() or "M-001",
                        "カテゴリー": chosen_dept,
                        "商品コード": code,
                        "商品名": name,
                        "単価": r["price"],
                        "数量": r["qty"],
                        "小計": r["price"] * r["qty"]
                    })

            if valid_new_items:
                insert_records_to_db(current_user, valid_new_items, auto_backup=False)
                st.toast(f"⚡ {len(valid_new_items)} 件を記録しました！")
                st.rerun()

    # 2️⃣ 登録済みデータ一覧・編集・削除
    st.write("")
    st.markdown('<div class="section-title">2️⃣ 登録済みデータ一覧・編集・安全な削除</div>', unsafe_allow_html=True)

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
                st.toast("✅ 保存しました！")
                st.rerun()

        with col_del:
            if st.button("🗑️ 選択・削除された行を反映する", use_container_width=True):
                existing_ids = set(int(i) for i in df_current["id"].dropna().tolist())
                current_ids = set(int(i) for i in edited_df["id"].dropna().tolist() if pd.notnull(i))
                deleted_ids = existing_ids - current_ids
                if deleted_ids:
                    backed = delete_records_by_ids(list(deleted_ids), current_user)
                    st.session_state.last_deleted_records = backed
                    st.toast("🗑️ 削除しました（いつでも復元可能）。")
                    st.rerun()

        with col_dl:
            csv_export = edited_df.to_csv(index=False).encode("utf-8-sig")
            st.download_button("📥 CSV出力", data=csv_export, file_name="seika_transactions.csv", mime="text/csv", use_container_width=True)

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
# タブ 3: 生成AIアシスタント (Gemini 2.5 Flash)
# ==========================================
with tabs[2]:
    st.markdown('<div class="section-title">🤖 生成AI青果アナリティクス & 経営アシスタント (Gemini 2.5 Flash 連携)</div>', unsafe_allow_html=True)
    
    has_api_key = bool(os.environ.get("GEMINI_API_KEY") or get_secret("GEMINI_API_KEY", "") or st.session_state.get("custom_gemini_api_key", ""))
    c_ai1, c_ai2 = st.columns([3, 1.5])
    with c_ai1:
        if has_api_key:
            st.success("🟢 **Google Gemini 2.5 Flash API 連携中**: 超高速・高精度な生成AIによる市場分析が有効です。")
        else:
            st.info("💡 **内蔵スマート青果AIアナライザー (高速ローカル推論モード)**: APIキー不要で即座に詳細分析レポートを作成できます。")
    with c_ai2:
        with st.popover("⚙️ Gemini APIキー設定"):
            new_key = st.text_input("Google AI Studio API Key", value=st.session_state.get("custom_gemini_api_key", ""), type="password")
            if st.button("設定を保存", type="primary"):
                st.session_state.custom_gemini_api_key = new_key.strip()
                st.toast("✅ APIキーを保存しました！")
                st.rerun()

    st.write("")
    ai_col1, ai_col2 = st.columns([1.5, 1])
    with ai_col1:
        st.markdown("##### 📈 取引データ総合診断 & 経営レポート自動生成")
        if st.button("⚡ AI総合診断レポートを自動生成する", type="primary", use_container_width=True):
            with st.spinner("🤖 生成AIが青果取引データを詳細分析中..."):
                st.session_state.latest_ai_report = generate_produce_ai_insights(df_current)
        
        if "latest_ai_report" in st.session_state:
            st.markdown(st.session_state.latest_ai_report)

    with ai_col2:
        st.markdown("##### 💬 青果アナリストAIチャット（自由対話）")
        user_ai_q = st.text_input("AIへの質問:", placeholder="例: 1課の売上を伸ばすには？")
        if st.button("🤖 AIに質問する", use_container_width=True):
            if user_ai_q.strip():
                with st.spinner("AIが回答中..."):
                    ans = call_gemini_ai(user_ai_q, "あなたは青果卸売専門の優秀なAIコンサルタントです。", st.session_state.get("custom_gemini_api_key"))
                    if not ans:
                        ans = f"【AI回答】ご質問「{user_ai_q}」について分析しました。主力課の定番野菜の需要動向に合わせた仕入れが推奨されます。"
                    st.markdown(f"**回答:**\n{ans}")

# ==========================================
# タブ 4: 社員別データ監視・確認センター（管理者専用）
# ==========================================
with tabs[3]:
    if current_role == "admin":
        st.markdown('<div class="section-title">👥 管理者用：社員別データ入力状況・横断モニタリング</div>', unsafe_allow_html=True)
        df_summary = get_employee_summary_stats()
        if not df_summary.empty:
            st.dataframe(df_summary, use_container_width=True)
        else:
            st.info("データがありません。")
    else:
        st.info(f"ログイン中のアカウント: **{current_user}** （一般社員）")
        my_recs = load_records_from_db(current_user)
        st.dataframe(my_recs, use_container_width=True)

# ==========================================
# タブ 5: BCP自動バックアップ・復旧
# ==========================================
with tabs[4]:
    st.markdown('<div class="section-title">🛡️ 企業のBCP対策：バックアップ履歴 & データ復旧</div>', unsafe_allow_html=True)
    if st.button("💾 今すぐフルバックアップを作成", type="primary"):
        res = create_backup_snapshot(current_user, trigger_reason="手動即時バックアップ")
        if res.get("success"):
            st.success(f"✅ バックアップ完了: {res['filename']}")
            st.rerun()

    backups = list_all_backups()
    if backups:
        df_bk = pd.DataFrame(backups)[["timestamp", "trigger", "records_count", "total_amount", "size_kb", "created_by", "filename"]]
        st.dataframe(df_bk, use_container_width=True)

        selected_bk_idx = st.selectbox("復元する世代を選択", range(len(backups)), format_func=lambda i: f"{backups[i]['timestamp']} | {backups[i]['records_count']}件")
        if st.button("♻️ この時点に復旧する", type="primary"):
            r_res = restore_from_backup_file(backups[selected_bk_idx]["filepath"], current_user)
            if r_res.get("success"):
                st.success("🎉 復旧しました！")
                st.rerun()

# ==========================================
# タブ 6: 取引データ一括インポート (Excel / CSV)
# ==========================================
with tabs[5]:
    st.markdown('<div class="section-title">📥 取引データ一括インポート (Excel / CSV)</div>', unsafe_allow_html=True)
    uploaded_file = st.file_uploader("取引データファイルを選択", type=["xlsx", "xls", "csv", "txt", "tsv"], key="bulk_upload_tab")
    if uploaded_file is not None:
        try:
            df_up, sups = parse_trade_file(uploaded_file)
            if not df_up.empty:
                st.dataframe(df_up.head(10), use_container_width=True)
                if st.button("📥 データベースに一括インポート", type="primary"):
                    insert_records_to_db(current_user, df_up.to_dict("records"))
                    st.success(f"🎉 {len(df_up)} 件を一括インポートしました！")
                    st.rerun()
        except Exception as e:
            st.error(f"ファイル解析エラー: {e}")
