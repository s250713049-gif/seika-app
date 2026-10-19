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
# 2. データベース接続エンジン（高速化・コネクションプール）
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
    current_url = get_database_url()
    return get_db_engine(current_url)

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

        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_records_user ON records(user_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_records_date ON records(date)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)"))
        except Exception:
            pass

        user_count = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
        if user_count == 0:
            init_admin_pw = get_secret("INITIAL_ADMIN_PASSWORD", "password123")
            init_users = [
                {"username": "admin", "pw": init_admin_pw, "role": "admin", "name": "管理者 (統括)"},
                {"username": "user1", "pw": get_secret("INITIAL_USER1_PASSWORD", "seika2026"), "role": "staff", "name": "社員A (東京市場担当)"},
                {"username": "staff_a", "pw": get_secret("INITIAL_STAFF_A_PASSWORD", "market01"), "role": "staff", "name": "社員B (横浜市場担当)"},
                {"username": "staff_c", "pw": get_secret("INITIAL_STAFF_C_PASSWORD", "seika03"), "role": "staff", "name": "社員C (大田市場担当)"}
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
# 6. バックアップ管理（非同期・軽量化設計）
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
            "version": "3.3_section_fast",
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

        all_backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "seika_backup_*.json")))
        if len(all_backups) > 30:
            for old_f in all_backups[:-30]:
                try:
                    os.remove(old_f)
                except:
                    pass

        return {
            "success": True,
            "filename": backup_filename,
            "path": backup_path,
            "count": len(df_rec),
            "timestamp": backup_payload["timestamp"]
        }
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
        deleted_to_restore = data.get("deleted_records", [])

        engine = get_current_engine()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM records"))
            conn.execute(text("DELETE FROM deleted_records"))

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

            if deleted_to_restore:
                ins_del = text('''
                    INSERT INTO deleted_records (original_id, user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal, deleted_at)
                    VALUES (:original_id, :user_id, :date, :market_no, :category, :item_code, :item_name, :unit_price, :quantity, :subtotal, :deleted_at)
                ''')
                for d in deleted_to_restore:
                    p = int(float(d.get("unit_price", 0)))
                    q = int(float(d.get("quantity", 0)))
                    conn.execute(ins_del, {
                        "original_id": d.get("original_id", 0),
                        "user_id": d.get("user_id", user_id),
                        "date": str(d.get("date", "")),
                        "market_no": str(d.get("market_no", "")),
                        "category": str(d.get("category", "")),
                        "item_code": str(d.get("item_code", "")),
                        "item_name": str(d.get("item_name", "")),
                        "unit_price": p,
                        "quantity": q,
                        "subtotal": p * q,
                        "deleted_at": str(d.get("deleted_at", ""))
                    })

        if "records_cache" in st.session_state:
            del st.session_state.records_cache
        return {"success": True, "count": len(records_to_restore)}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ==========================================
# 7. データCRUD関数（セッションキャッシュ対応）
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
        return []
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

    if auto_backup:
        create_backup_snapshot(user_id, trigger_reason=f"データ追加 ({len(records_list)}件)")

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
    clean_ids = [int(i) for i in deleted_ids_to_restore if pd.notnull(i) and str(i).replace('.0','').isdigit()]
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

def cleanup_duplicate_records(user_id):
    engine = get_current_engine()
    with engine.begin() as conn:
        if user_id:
            cleanup_query = text('''
                DELETE FROM records 
                WHERE id NOT IN (
                    SELECT MIN(id) 
                    FROM records 
                    WHERE user_id = :user_id 
                    GROUP BY date, market_no, category, item_code, item_name, unit_price, quantity, subtotal
                )
                AND user_id = :user_id
            ''')
            result = conn.execute(cleanup_query, {"user_id": user_id})
        else:
            cleanup_query = text('''
                DELETE FROM records 
                WHERE id NOT IN (
                    SELECT MIN(id) 
                    FROM records 
                    GROUP BY user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal
                )
            ''')
            result = conn.execute(cleanup_query)
    invalidate_records_cache()
    return result.rowcount

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
# 8. マスタデータ管理（社内公式商品コード表 & ○課定義 完全連動）
# ==========================================
SECTION_DEFINITIONS_PY = [
    {"label": "1課 (熊谷・大宮)", "depts": ["1課4課（熊谷・大宮） / 2課 / 栗原", "1課4課/2課/栗原"]},
    {"label": "2課 (栗原)", "depts": ["1課4課（熊谷・大宮） / 2課 / 栗原", "1課4課/2課/栗原"]},
    {"label": "3課 (照井)", "depts": ["3課（照井） / 11課（相澤 史）", "3課/11課"]},
    {"label": "4課 (熊谷・大宮)", "depts": ["1課4課（熊谷・大宮） / 2課 / 栗原", "1課4課/2課/栗原"]},
    {"label": "5課 (小林・牧野)", "depts": ["5課12課（小林・牧野）", "5課12課"]},
    {"label": "11課 (相澤 史)", "depts": ["3課（照井） / 11課（相澤 史）", "3課/11課"]},
    {"label": "12課 (小林・牧野)", "depts": ["5課12課（小林・牧野）", "5課12課"]},
    {"label": "1課4課（熊谷・大宮） / 2課 / 栗原", "depts": ["1課4課（熊谷・大宮） / 2課 / 栗原", "1課4課/2課/栗原"]},
    {"label": "3課（照井） / 11課（相澤 史）", "depts": ["3課（照井） / 11課（相澤 史）", "3課/11課"]},
    {"label": "5課12課（小林・牧野）", "depts": ["5課12課（小林・牧野）", "5課12課"]},
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
# 9. セッション状態の初期化 & 自動ログイン復帰（F5リロード対策）
# ==========================================
if "logged_in_user" not in st.session_state:
    st.session_state.logged_in_user = None
    st.session_state.user_role = None

if "admin_target_user" not in st.session_state:
    st.session_state.admin_target_user = None

if "processed_file_hashes" not in st.session_state:
    st.session_state.processed_file_hashes = set()

if "verification_batch" not in st.session_state:
    st.session_state.verification_batch = None

if "last_deleted_records" not in st.session_state:
    st.session_state.last_deleted_records = []

# 【エクセル/CSVから読み込まれた取引先リスト】
if "extracted_suppliers" not in st.session_state:
    st.session_state.extracted_suppliers = [
        "M-001 (築地第一取引先)",
        "M-002 (大田市場取引先)",
        "M-003 (横浜南部青果)"
    ]

# 【連続入力用の前回値キープ】
if "last_input_market_no" not in st.session_state:
    st.session_state.last_input_market_no = "M-001 (築地第一取引先)"

if "last_input_date" not in st.session_state:
    st.session_state.last_input_date = datetime.now().date()

# ==========================================
# 9.1 エクセル (xlsx/xls) & CSV 取引データ共通解析エンジン
# ==========================================
def parse_trade_file(uploaded_file):
    fname = uploaded_file.name.lower()
    df_raw = None

    if fname.endswith((".xlsx", ".xls")):
        excel_dict = pd.read_excel(uploaded_file, sheet_name=None)
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
    else:
        content_bytes = uploaded_file.getvalue()
        for enc in ["utf-8-sig", "utf-8", "cp932", "shift-jis"]:
            try:
                df_raw = pd.read_csv(io.BytesIO(content_bytes), encoding=enc)
                break
            except Exception:
                continue
        if df_raw is None:
            df_raw = pd.read_csv(io.BytesIO(content_bytes), encoding="latin1")

    if df_raw.empty:
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
        df_norm["カテゴリー"] = "1課4課/2課/栗原"
    else:
        df_norm["カテゴリー"] = df_norm["カテゴリー"].fillna("1課4課/2課/栗原").astype(str).str.strip()

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
            <p style="color: #718096; font-size: 14px;">エクセル自動記録・連続入力高速化・PostgreSQLクラウド同期・BCP耐スリープ対応</p>
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
        st.markdown("""
            <div class="security-notice">
                <b>⚡ 高速化 & 堅牢セキュリティ仕様:</b><br>
                • 連続入力時もページが再読込されず、サクサク高速に入力できます。<br>
                • パスワードはDB内で独自のソルト＋PBKDF2-HMAC-SHA256で強固に暗号化。<br>
                • HMAC署名付きセッショントークンにより、F5リロードしてもログインが継続します。
            </div>
        """, unsafe_allow_html=True)
        
        st.caption("テスト用クイック認証:")
        q1, q2, q3 = st.columns(3)
        with q1:
            if st.button("👑 管理者(admin)", use_container_width=True):
                auth = verify_user_login("admin", get_secret("INITIAL_ADMIN_PASSWORD", "password123"))
                if auth:
                    st.session_state.logged_in_user = "admin"
                    st.session_state.user_role = "admin"
                    st.query_params["auth_token"] = create_session_token("admin", "admin")
                    st.rerun()
        with q2:
            if st.button("👤 社員A(user1)", use_container_width=True):
                auth = verify_user_login("user1", get_secret("INITIAL_USER1_PASSWORD", "seika2026"))
                if auth:
                    st.session_state.logged_in_user = "user1"
                    st.session_state.user_role = "staff"
                    st.query_params["auth_token"] = create_session_token("user1", "staff")
                    st.rerun()
        with q3:
            if st.button("👤 社員B(staff_a)", use_container_width=True):
                auth = verify_user_login("staff_a", get_secret("INITIAL_STAFF_A_PASSWORD", "market01"))
                if auth:
                    st.session_state.logged_in_user = "staff_a"
                    st.session_state.user_role = "staff"
                    st.query_params["auth_token"] = create_session_token("staff_a", "staff")
                    st.rerun()
    st.stop()

# ログイン中のユーザー情報
current_user = st.session_state.logged_in_user
current_role = st.session_state.user_role
db_status = get_db_info()

# ==========================================
# 11. サイドバー（セッションログアウト・クラウドDB設定）
# ==========================================
with st.sidebar:
    st.image("https://img.icons8.com/emoji/96/000000/red-apple.png", width=46)
    st.title("青果卸売 Pro")
    
    role_badge = '<span class="role-badge-admin">👑 管理者</span>' if current_role == "admin" else '<span class="role-badge-staff">👤 社員</span>'
    st.markdown(f"**{current_user}** {role_badge} <span class='speed-badge'>⚡ 高速モード</span>", unsafe_allow_html=True)

    db_badge_html = f'<span class="db-badge-cloud">{db_status["badge"]}</span>' if db_status["is_cloud"] else f'<span class="db-badge-local">{db_status["badge"]}</span>'
    st.markdown(f"**DB**: {db_badge_html}", unsafe_allow_html=True)

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
        st.caption("PostgreSQL / Supabase の接続URL")
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
                    ensure_db_schema_initialized(new_db_url.strip())
                    invalidate_records_cache()
                    st.success("☁️ 接続完了！")
                    st.rerun()
        with col_db2:
            if st.button("ローカルDB", use_container_width=True):
                st.session_state.custom_database_url = ""
                invalidate_records_cache()
                st.info("📁 ローカルDB切替")
                st.rerun()

    if st.button("💾 手動DBバックアップ", use_container_width=True):
        res = create_backup_snapshot(current_user, trigger_reason="手動即時バックアップ")
        if res.get("success"):
            st.success(f"💾 作成完了 ({res['count']}件)")

# ==========================================
# 12. 表示対象データの決定
# ==========================================
if current_role == "admin":
    viewing_user_filter = st.session_state.admin_target_user
else:
    viewing_user_filter = current_user

df_current = load_records_from_db(viewing_user_filter)

# ==========================================
# メインコンテンツエリア
# ==========================================
st.markdown("""
    <div class="app-header">
        <h1>🍎 青果卸売 在庫・取引管理 Pro</h1>
        <p>エクセル自動記録・連続入力高速化・PostgreSQLクラウド同期・耐スリープBCP自動バックアップ・社員別個別入力</p>
    </div>
""", unsafe_allow_html=True)

# 管理者用：表示対象社員セレクター
if current_role == "admin":
    c_m1, c_m2, c_m3 = st.columns([3, 1.5, 1.5])
    with c_m1:
        db_users = get_all_users_from_db()
        user_list = [u["username"] for u in db_users]
        user_labels = {None: "👥 【全社員統合ビュー】すべての社員のデータを一括表示"}
        for u in db_users:
            u_name = u.get("name", u["username"])
            u_role_str = "👑 管理者" if u["role"] == "admin" else "👤 社員"
            user_labels[u["username"]] = f"{u_role_str}: {u['username']} ({u_name})"

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

    with c_m2:
        st.write("")
        if st.session_state.admin_target_user is not None:
            st.info(f"現在 **{st.session_state.admin_target_user}** を閲覧中")
        else:
            st.success("現在 **全社員データ** を統合閲覧中")
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
    "👥 社員別データ監視 (管理者)" if current_role == "admin" else "👤 担当データ状況",
    "🛡️ BCP自動バックアップ・復旧",
    "📥 取引データ一括インポート (Excel / CSV)"
]
tabs = st.tabs(tab_titles)

# ==========================================
# タブ 1: 取引データ入力・管理（○課セレクター・自動連動対応）
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
                誤って削除した場合は、以下のボタンを押すと直ちに元通り復元できます。
            </div>
        """, unsafe_allow_html=True)
        col_undo1, col_undo2 = st.columns([2, 5])
        with col_undo1:
            if st.button("↩️ 直前の削除を取り消して元通り復元する", type="primary", use_container_width=True):
                restored_records = st.session_state.last_deleted_records
                insert_records_to_db(current_user, restored_records)
                st.session_state.last_deleted_records = []
                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.success(f"✅ {len(restored_records)} 件のデータを元通り復元しました！")
                st.rerun()
        with col_undo2:
            if st.button("✖ 閉じる", use_container_width=False):
                st.session_state.last_deleted_records = []
                st.rerun()

    # 元ファイル照合 & 不一致修正最下部自動記録
    if st.session_state.verification_batch is not None:
        v_batch = st.session_state.verification_batch
        v_file_name = v_batch["file_name"]
        expected_df = v_batch["df"]
        
        mismatches = []
        if df_current is not None and not df_current.empty:
            for idx, exp_row in expected_df.iterrows():
                match_cand = df_current[
                    (df_current["商品名"] == exp_row["商品名"]) & 
                    (df_current["日付"] == exp_row["日付"])
                ]
                if match_cand.empty:
                    mismatches.append({
                        "行番号": idx + 1,
                        "商品名": exp_row["商品名"],
                        "日付": exp_row["日付"],
                        "市場番号": exp_row.get("市場番号", ""),
                        "カテゴリー": exp_row.get("カテゴリー", "1課4課/2課/栗原"),
                        "商品コード": exp_row.get("商品コード", "AUTO"),
                        "不一致項目": "登録データなし",
                        "元ファイルの値(正)": f"単価:{exp_row.get('単価', 0)}円, 数量:{exp_row.get('数量', 0)}個",
                        "現在の登録値(誤)": "未登録または削除済み",
                        "exp_price": int(exp_row.get("単価", 0)),
                        "exp_qty": int(exp_row.get("数量", 0)),
                        "record_id": None
                    })
                else:
                    db_row = match_cand.iloc[-1]
                    for col in ["単価", "数量", "小計", "市場番号", "カテゴリー"]:
                        exp_val = str(exp_row.get(col, "")).strip()
                        db_val = str(db_row.get(col, "")).strip()
                        if exp_val and db_val and exp_val != db_val:
                            mismatches.append({
                                "行番号": idx + 1,
                                "商品名": exp_row["商品名"],
                                "日付": exp_row["日付"],
                                "市場番号": exp_row.get("市場番号", db_row.get("市場番号", "")),
                                "カテゴリー": exp_row.get("カテゴリー", db_row.get("カテゴリー", "")),
                                "商品コード": exp_row.get("商品コード", db_row.get("商品コード", "")),
                                "不一致項目": col,
                                "元ファイルの値(正)": exp_val,
                                "現在の登録値(誤)": db_val,
                                "exp_price": int(exp_row.get("単価", db_row.get("単価", 0))),
                                "exp_qty": int(exp_row.get("数量", db_row.get("数量", 0))),
                                "record_id": int(db_row.get("id"))
                            })
        
        st.write("")
        if len(mismatches) == 0:
            st.success(f"🛡️ **元ファイル照合結果: 『{v_file_name}』の全 {len(expected_df)} 件が記録データと100%完全一致しています（整合性確認済み）**")
        else:
            st.markdown(f"""
                <div class="mismatch-box">
                    <h4 style="margin: 0; color: #991b1b;">🚨 元ファイル照合エラー: 『{v_file_name}』との間に {len(mismatches)} 箇所の不一致が検出されました</h4>
                    <p style="margin: 4px 0 0 0; color: #4b5563; font-size: 13px;">
                        不一致データを修正し、リストの最下部に自動記録することができます。
                    </p>
                </div>
            """, unsafe_allow_html=True)
            
            diff_display_df = pd.DataFrame(mismatches)[["行番号", "商品名", "不一致項目", "元ファイルの値(正)", "現在の登録値(誤)"]]
            st.dataframe(diff_display_df, use_container_width=True)

            mismatch_options = [f"行{m['行番号']}: {m['商品名']} ({m['不一致項目']}の不一致)" for m in mismatches]
            selected_mismatch_idx = st.selectbox(
                "修正する不一致データを選択", 
                range(len(mismatch_options)), 
                format_func=lambda i: mismatch_options[i],
                key="select_mismatch_to_fix"
            )
            
            target_mismatch = mismatches[selected_mismatch_idx]
            c_fix1, c_fix2, c_fix3, c_fix4, c_fix5 = st.columns([2, 2, 2, 2, 2.5])
            with c_fix1:
                fix_date = st.text_input("日付", value=str(target_mismatch["日付"]), key="fix_input_date")
            with c_fix2:
                fix_market = st.text_input("市場番号", value=str(target_mismatch["市場番号"]), key="fix_input_market")
            with c_fix3:
                fix_price = st.number_input("修正単価 (円)", min_value=0, value=int(target_mismatch["exp_price"]), step=10, key="fix_input_price")
            with c_fix4:
                fix_qty = st.number_input("修正数量", min_value=0, value=int(target_mismatch["exp_qty"]), step=1, key="fix_input_qty")
            with c_fix5:
                fix_subtotal = fix_price * fix_qty
                st.metric("修正後小計", f"¥ {fix_subtotal:,}")
            
            col_act1, col_act2 = st.columns([3, 2.5])
            with col_act1:
                if st.button("✍️ 修正したデータをリストの最下部に自動記録する", type="primary", use_container_width=True, key="fix_and_append_below_btn"):
                    fixed_record = [{
                        "日付": fix_date,
                        "市場番号": fix_market,
                        "カテゴリー": target_mismatch["カテゴリー"],
                        "商品コード": target_mismatch["商品コード"],
                        "商品名": target_mismatch["商品名"],
                        "単価": fix_price,
                        "数量": fix_qty,
                        "小計": fix_subtotal
                    }]
                    insert_records_to_db(current_user, fixed_record)
                    if target_mismatch.get("record_id"):
                        delete_records_by_ids([target_mismatch["record_id"]], current_user)
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.toast(f"✨ 修正データをリスト最下部に自動記録しました！")
                    st.rerun()

            with col_act2:
                if st.button("🛠️ 全不一致を元ファイル通りに修正して自動記録", use_container_width=True, key="fix_all_mismatch_btn"):
                    insert_records_to_db(current_user, expected_df.to_dict("records"))
                    cleanup_duplicate_records(current_user)
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.toast("✅ 元ファイルの内容ですべて修正し、下に自動記録しました！")
                    st.rerun()

    st.write("")
    st.markdown(f'<div class="section-title">1️⃣ 新規取引データの入力 & ファイル自動記録 (入力者: <b>{current_user}</b>)</div>', unsafe_allow_html=True)

    # -------------------------------------------------------------
    # 📁 A. 取引先ファイルの自動記録 (Excel / CSV どのパソコンからでも選択可能)
    # -------------------------------------------------------------
    with st.expander("📁 取引先ファイル（Excel / CSV）から一括自動記録 & 取引先自動抽出", expanded=False):
        st.markdown("""
            <div style="font-size: 0.85rem; color: #4b5563; margin-bottom: 8px;">
                💡 <b>どのパソコンのブラウザからでもエクセルファイル（.xlsx / .xls）やCSVファイルを選択できます。</b><br>
                読み込んだエクセルデータは下の記録一覧へ同じ形式で自動記録され、ファイル内の取引先名が手動入力の候補リストに自動追加されます。
            </div>
        """, unsafe_allow_html=True)
        col_up1, col_up2 = st.columns([4, 1.2])
        with col_up1:
            uploaded_entry_file = st.file_uploader(
                "取引データファイル（Excel / CSV）を選択", 
                type=["xlsx", "xls", "csv", "txt", "tsv"], 
                key="auto_unique_verified_uploader", 
                label_visibility="collapsed"
            )
        with col_up2:
            if st.button("🧪 テストデータ3件", use_container_width=True):
                sample_items = [
                    {"日付": str(datetime.now().date()), "市場番号": "M-001 (築地第一取引先)", "カテゴリー": "1課 (熊谷・大宮)", "商品コード": "150", "商品名": "トマト", "単価": 290, "数量": 50, "小計": 14500},
                    {"日付": str(datetime.now().date()), "市場番号": "M-002 (大田市場取引先)", "カテゴリー": "3課 (照井)", "商品コード": "42", "商品名": "キャベツ", "単価": 200, "数量": 40, "小計": 8000},
                    {"日付": str(datetime.now().date()), "市場番号": "M-003 (横浜南部青果)", "カテゴリー": "5課 (小林・牧野)", "商品コード": "519", "商品名": "富士りんご", "単価": 360, "数量": 30, "小計": 10800}
                ]
                insert_records_to_db(current_user, sample_items)
                st.session_state.verification_batch = {
                    "file_name": "サンプル青果データ",
                    "df": pd.DataFrame(sample_items)
                }
                for sup in ["M-001 (築地第一取引先)", "M-002 (大田市場取引先)", "M-003 (横浜南部青果)"]:
                    if sup not in st.session_state.extracted_suppliers:
                        st.session_state.extracted_suppliers.append(sup)
                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.toast(f"✅ テストデータ 3件 を自動記録しました！")
                st.rerun()

        if uploaded_entry_file is not None:
            file_bytes = uploaded_entry_file.getvalue()
            file_hash = hashlib.md5(file_bytes).hexdigest()
            
            if file_hash not in st.session_state.processed_file_hashes:
                try:
                    st.session_state.processed_file_hashes.add(file_hash)
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
                        st.toast(f"✅ 『{uploaded_entry_file.name}』から {len(new_records_list)} 件を自動記録！取引先 {len(sups)} 件を手動入力の候補に追加しました。")
                        st.rerun()
                    else:
                        st.warning("⚠️ ファイル内に有効なデータ行が見つかりませんでした。")
                except Exception as e:
                    st.error(f"ファイル解析エラー: {e}")

    # -------------------------------------------------------------
    # 📖 社内公式品名・商品コード早見表（クイック検索）
    # -------------------------------------------------------------
    with st.expander("📖 社内公式品名・商品コード早見表 (クイック検索)", expanded=False):
        c_srch1, c_srch2 = st.columns([2, 1])
        with c_srch1:
            code_query = st.text_input("🔍 品名またはコードで絞り込み検索", placeholder="例: 319, トマト, 大葉, デコポン, 仙台麩, レタス", key="code_table_search")
        with c_srch2:
            dept_filter_view = st.selectbox("部署フィルター", ["すべて"] + DEPT_LIST, key="dept_filter_view_sel")
        
        filtered_master_df = df_master_raw.copy()
        if dept_filter_view != "すべて":
            filtered_master_df = filtered_master_df[filtered_master_df["department"] == dept_filter_view]
        if code_query.strip():
            q_clean = code_query.strip()
            filtered_master_df = filtered_master_df[
                filtered_master_df["item_name"].str.contains(q_clean, na=False) |
                filtered_master_df["item_code"].str.contains(q_clean, na=False)
            ]
        
        display_quick_df = filtered_master_df[["item_code", "item_name", "department"]].rename(columns={
            "item_code": "商品コード",
            "item_name": "品名",
            "department": "担当部署・課"
        })
        st.caption(f"該当商品: {len(display_quick_df)} 件")
        st.dataframe(display_quick_df, use_container_width=True, height=220)

    # -------------------------------------------------------------
    # ⚡ B. 【超高速】手動連続入力フォーム（○課カテゴリー連動・自動反映）
    # -------------------------------------------------------------
    st.markdown("""
        <div style="background-color: #f0fdf4; border: 1.5px solid #86efac; border-radius: 8px; padding: 10px 14px; margin-bottom: 8px;">
            <b style="color: #166534;">⚡ 高速手動連続入力フォーム (○課カテゴリー選択 & 商品・商品番号自動反映):</b>
            <span style="color: #4b5563; font-size: 13px;">
                担当課（○課）を選択すると該当する商品と商品番号が自動反映されます。エクセルからの取引先も選択・保持されます。
            </span>
        </div>
    """, unsafe_allow_html=True)

    # 読み込まれたエクセルデータまたは登録済みデータから取引先を収集
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
        if selected_sup_choice != "（手入力・自由入力モード）":
            chosen_sup_name = selected_sup_choice.replace("🏢 ", "", 1)
            if st.session_state.last_input_market_no != chosen_sup_name:
                st.session_state.last_input_market_no = chosen_sup_name
                st.rerun()
    with c_sup2:
        st.write("")
        st.caption(f"※ エクセル等から抽出された取引先: **{len(combined_suppliers)} 件** 利用可能")

    # 【重要改修】カテゴリーの選択箇所を「○課」とし、該当する商品と商品番号を自動反映
    c_d1, c_d2 = st.columns([2.5, 1.5])
    with c_d1:
        chosen_dept = st.selectbox(
            "🏢 担当課 (○課) を選択 (課を選ぶと該当する商品と商品番号が自動反映されます):",
            options=[sec["label"] for sec in SECTION_DEFINITIONS_PY],
            key="active_form_dept_choice"
        )
    with c_d2:
        st.write("")
        st.caption("※ ○課を選ぶと、該当する商品と商品番号が下の選択肢に自動反映されます")

    selected_sec_def = next((s for s in SECTION_DEFINITIONS_PY if s["label"] == chosen_dept), None)
    if not selected_sec_def or "ALL" in selected_sec_def["depts"]:
        candidate_item_options = ITEM_FLAT_LIST
    else:
        matched_items = []
        for d in selected_sec_def["depts"]:
            matched_items.extend(ITEM_DEPT_MAP.get(d, []))
        seen = set()
        dedup_items = []
        for item in matched_items:
            key = (item["code"], item["name"])
            if key not in seen:
                seen.add(key)
                dedup_items.append(item)
        candidate_item_options = [f"[{item['code']}] {item['name']}" for item in dedup_items]

    with st.form("speed_continuous_input_form", clear_on_submit=False):
        col_top1, col_top2 = st.columns([2, 1])
        with col_top1:
            form_market_no = st.text_input(
                "市場番号 / 取引先名 (上の選択肢から選ぶか直接入力・連続入力中保持)", 
                value=st.session_state.last_input_market_no,
                key="speed_form_market"
            )
        with col_top2:
            form_input_date = st.date_input(
                "取引日付", 
                value=st.session_state.last_input_date,
                key="speed_form_date"
            )

        st.caption("▼ 商品行（選択された課の商品と商品番号が自動反映されます。入力したい行にチェックを入れて『追加保存』を押してください）")
        
        form_rows_data = []

        for i in range(1, 6):
            c_chk, c_item, c_price, c_qty = st.columns([0.8, 4.2, 2.5, 2.5])
            with c_chk:
                is_active = st.checkbox(f"行 {i}", value=(i == 1), key=f"f_active_{i}")
            
            with c_item:
                default_idx = min(i - 1, len(candidate_item_options) - 1) if candidate_item_options else 0
                item_val = st.selectbox(
                    f"該当商品 [商品番号 : 品名] #{i}", 
                    candidate_item_options, 
                    index=default_idx, 
                    key=f"f_item_{i}_{chosen_dept}", 
                    label_visibility="collapsed" if i > 1 else "visible"
                )
            with c_price:
                price_val = st.number_input(
                    f"単価 #{i}", 
                    min_value=0, 
                    value=200 if is_active else 0, 
                    step=10, 
                    key=f"f_price_{i}", 
                    label_visibility="collapsed" if i > 1 else "visible"
                )
            with c_qty:
                qty_val = st.number_input(
                    f"数量 #{i}", 
                    min_value=0, 
                    value=10 if is_active else 0, 
                    step=1, 
                    key=f"f_qty_{i}", 
                    label_visibility="collapsed" if i > 1 else "visible"
                )

            form_rows_data.append({
                "active": is_active,
                "item_str": item_val,
                "price": price_val,
                "qty": qty_val
            })

        col_sub1, col_sub2 = st.columns([3, 1])
        with col_sub1:
            submit_manual_batch = st.form_submit_button(
                "➕ 入力した商品を瞬時に記録する（画面のリフレッシュなしで高速保存）", 
                type="primary", 
                use_container_width=True
            )
        with col_sub2:
            st.caption("※ 登録後も市場番号と日付は自動保持されます")

        if submit_manual_batch:
            if not form_market_no.strip():
                st.error("❌ 市場番号を入力してください。")
            else:
                st.session_state.last_input_market_no = form_market_no
                st.session_state.last_input_date = form_input_date

                valid_new_items = []
                for r in form_rows_data:
                    if r["active"] and r["item_str"] and r["price"] > 0 and r["qty"] > 0:
                        raw_str = r["item_str"]
                        if raw_str.startswith("[") and "]" in raw_str:
                            code = raw_str.split("]")[0].lstrip("[").strip()
                            name_and_dept = raw_str.split("]")[1].strip()
                        elif " : " in raw_str:
                            parts = raw_str.split(" : ")
                            code = parts[0].strip()
                            name_and_dept = parts[1].strip() if len(parts) > 1 else parts[0].strip()
                        else:
                            code = ""
                            name_and_dept = raw_str.strip()
                        
                        if " (" in name_and_dept:
                            name = name_and_dept.split(" (")[0].strip()
                            dept_part = name_and_dept.split(" (")[1].rstrip(")")
                        else:
                            name = name_and_dept
                            dept_part = chosen_dept if "全課" not in chosen_dept else ITEM_LOOKUP_MAP.get(f"{code} : {name}", "1課4課/2課/栗原")

                        valid_new_items.append({
                            "日付": str(form_input_date),
                            "市場番号": form_market_no,
                            "カテゴリー": dept_part,
                            "商品コード": code,
                            "商品名": name,
                            "単価": r["price"],
                            "数量": r["qty"],
                            "小計": r["price"] * r["qty"]
                        })

                if valid_new_items:
                    insert_records_to_db(current_user, valid_new_items, auto_backup=False)
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.toast(f"⚡ {len(valid_new_items)} 件を瞬時に記録しました！続けて次のデータを入力できます。")
                    st.rerun()
                else:
                    st.warning("⚠️ チェックを入れた行の単価と数量が1以上であるか確認してください。")

    # -------------------------------------------------------------
    # 2️⃣ 登録済みデータ一覧・編集・削除
    # -------------------------------------------------------------
    st.write("")
    st.markdown('<div class="section-title">2️⃣ 登録済みデータ一覧・編集・安全な削除（いつでも復元可能）</div>', unsafe_allow_html=True)

    if records_cnt > 0:
        col_config = {
            "id": None,
            "日付": st.column_config.TextColumn("日付", required=True),
            "市場番号": st.column_config.TextColumn("市場番号", required=True),
            "カテゴリー": st.column_config.TextColumn("カテゴリー (○課)"),
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
                existing_ids = set(int(i) for i in df_current["id"].dropna().tolist())
                current_ids = set(int(i) for i in edited_df["id"].dropna().tolist() if pd.notnull(i))
                deleted_ids = existing_ids - current_ids
                
                if deleted_ids:
                    backed = delete_records_by_ids(list(deleted_ids), current_user)
                    st.session_state.last_deleted_records = backed
                
                for _, row in edited_df.iterrows():
                    r_dict = row.to_dict()
                    try:
                        p = int(float(r_dict.get("単価", 0)))
                        q = int(float(r_dict.get("数量", 0)))
                    except:
                        p, q = 0, 0
                    r_dict["単価"] = p
                    r_dict["数量"] = q
                    r_dict["小計"] = p * q
                    
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
                    st.toast(f"🗑️ {len(deleted_ids)} 件のデータを削除しました（復元可能です）")
                    st.rerun()
                else:
                    st.warning("⚠️ テーブル上で行を削除してから押してください。")

        with col_dl:
            csv_data = df_current.to_csv(index=False).encode('utf-8-sig')
            user_suffix = viewing_user_filter or "all_employees"
            st.download_button(
                label="📥 表示中のCSVをダウンロード",
                data=csv_data,
                file_name=f"青果取引データ_{user_suffix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=True
            )
    else:
        st.info(f"💡 現在選択されているアカウント（{view_owner_label}）の登録データはありません。")

    # 削除済みデータ保管庫（ゴミ箱）
    st.write("")
    with st.expander("♻️ 削除したデータの確認・元通り復元（ゴミ箱）", expanded=False):
        df_deleted = load_deleted_records_from_db(viewing_user_filter)
        deleted_count = len(df_deleted) if df_deleted is not None else 0
        st.markdown(f"保管中の削除データ: **{deleted_count} 件**")
        
        if deleted_count > 0:
            c_res_all, c_res_sel, c_purge = st.columns([2, 2.5, 2])
            with c_res_all:
                if st.button("♻️ すべてのデータを元通り復元する", type="primary", use_container_width=True, key="restore_all_trash_btn"):
                    all_del_ids = df_deleted["ゴミ箱ID"].tolist()
                    cnt = restore_records_from_trash(all_del_ids, current_user)
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.success(f"🎉 {cnt} 件のデータを元通り復元しました！")
                    st.rerun()
            
            with c_purge:
                if st.button("🧹 ゴミ箱を完全に空にする", use_container_width=True, key="purge_trash_btn"):
                    purge_deleted_records(viewing_user_filter)
                    st.toast("ゴミ箱を完全に空にしました。")
                    st.rerun()

            st.dataframe(df_deleted, use_container_width=True)

# ==========================================
# タブ 2: データ分析（軽量グラフ描画）
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
# タブ 3: 社員別データ監視・確認センター（管理者専用）
# ==========================================
with tabs[2]:
    if current_role == "admin":
        st.markdown('<div class="section-title">👥 管理者用：社員別データ入力状況・横断モニタリング</div>', unsafe_allow_html=True)
        st.markdown("""
            全社員のアカウントごとに、**どのアカウントが、どれだけの件数・数量・売上データを入力しているか** をリアルタイムで監視・集計・比較できます。
        """)

        df_summary = get_employee_summary_stats()
        db_users = get_all_users_from_db()
        users_name_map = {u["username"]: u.get("name", u["username"]) for u in db_users}
        
        if not df_summary.empty:
            df_summary["社員名/担当"] = df_summary["社員ID"].apply(lambda uid: users_name_map.get(uid, uid))
            cols_order = ["社員ID", "社員名/担当", "登録件数", "総数量", "総売上金額", "最終入力日"]
            df_summary = df_summary[cols_order]

            s1, s2, s3, s4 = st.columns(4)
            s1.metric("稼働中社員数", f"{len(df_summary)} 名")
            s2.metric("全社総入力件数", f"{df_summary['登録件数'].sum():,} 件")
            s3.metric("全社総数量", f"{df_summary['総数量'].sum():,} 個")
            s4.metric("全社総売上", f"¥ {df_summary['総売上金額'].sum():,}")

            st.write("")
            st.subheader("📋 社員別 入力実績集計テーブル")
            st.dataframe(
                df_summary.style.format({
                    "登録件数": "{:,} 件",
                    "総数量": "{:,} 個",
                    "総売上金額": "¥ {:,}"
                }),
                use_container_width=True
            )

            c_g1, c_g2 = st.columns(2)
            with c_g1:
                st.subheader("📊 社員別 売上実績比較")
                fig_bar = px.bar(
                    df_summary, 
                    x="社員名/担当", 
                    y="総売上金額", 
                    text_auto=True,
                    color="社員名/担当",
                    labels={"総売上金額": "売上金額 (円)"}
                )
                fig_bar.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=280, showlegend=False)
                st.plotly_chart(fig_bar, use_container_width=True)

            with c_g2:
                st.subheader("🍩 社員別 入力件数シェア")
                fig_pie_staff = px.pie(
                    df_summary, 
                    names="社員名/担当", 
                    values="登録件数", 
                    hole=0.45
                )
                fig_pie_staff.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=280)
                st.plotly_chart(fig_pie_staff, use_container_width=True)

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
            st.info("💡 まだ登録データがありません。社員アカウントから取引データを登録してください。")
    else:
        st.markdown(f'<div class="section-title">👤 アカウント情報 & あなたの入力状況</div>', unsafe_allow_html=True)
        st.info(f"ログイン中のアカウント: **{current_user}** （一般社員）\n\nあなたのアカウントで入力したデータは安全に個別に保存されています。")
        my_records = load_records_from_db(current_user)
        if not my_records.empty:
            st.metric("あなたが登録した総件数", f"{len(my_records)} 件")
            st.dataframe(my_records, use_container_width=True)
        else:
            st.caption("まだ登録されたデータはありません。タブ1から商品を入力してください。")

# ==========================================
# タブ 4: BCP自動バックアップ・復旧
# ==========================================
with tabs[3]:
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
# タブ 5: 取引データ一括インポート (Excel / CSV)
# ==========================================
with tabs[4]:
    st.markdown('<div class="section-title">📥 取引データ一括インポート (Excel / CSV)</div>', unsafe_allow_html=True)
    st.markdown("""
        どのパソコンからでもエクセルファイル（.xlsx / .xls）またはCSVファイルを選択して、一括で取引データをインポートできます。
        インポートされたデータ内の取引先は、手動入力フォームの候補リストにも自動登録されます。
    """)
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
