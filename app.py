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
    /* 不要なスピナーやレイアウトシフトを低減して高速化 */
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
        # クラウドDB接続時：高速プール管理
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

# 【高速化】毎回のリランでDDLを実行せず、アプリ起動時または明示呼出時のみ1回実行
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

        # 初期アカウント
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
            "version": "3.1_fast",
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
# 7. データCRUD関数（セッションキャッシュ対応で超高速化）
# ==========================================
def invalidate_records_cache():
    if "records_cache" in st.session_state:
        del st.session_state.records_cache

def load_records_from_db(user_id=None, force_reload=False):
    """【大幅高速化】セッションキャッシュを活用し、連続入力時に毎秒DBクエリを投げない"""
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
    """【高速化】連続入力時に重くならないよう、デフォルトの auto_backup は False に設定"""
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
# 8. マスタデータ（コード表の課別商品をプログラム内に内蔵）
# ==========================================
BUILTIN_ITEM_GROUPS = [
    {
        "departments": ["1課（熊谷）", "4課（大宮）", "2課", "栗原"],
        "items": [
            ("11", "かぶ"),
            ("130", "胡瓜"),
            ("233", "アーリーレッド"),
            ("319", "青唐辛子"),
            ("319", "トレビス"),
            ("57", "スリムネギ"),
            ("143", "茄子"),
            ("262", "赤唐辛子"),
            ("319", "みょうが竹"),
            ("319", "あみたけ"),
            ("66", "水耕みつば"),
            ("155", "ピーマン"),
            ("288", "カイワレ"),
            ("319", "食用菊系"),
            ("319", "ラーメン"),
            ("63", "根みつば"),
            ("268", "大葉"),
            ("288", "豆苗"),
            ("319", "食用花系"),
            ("319", "ルッコラ"),
            ("69", "にら"),
            ("126", "水菜"),
            ("288", "ブロスプ"),
            ("319", "山菜系"),
            ("319", "MIXマッシュ"),
            ("71", "ほうれん草"),
            ("87", "クレソン"),
            ("288", "レッドスプ"),
            ("319", "銀杏"),
            ("319", "ぬか"),
            ("71", "ちぢみほうれん草"),
            ("108", "菜の花"),
            ("288", "ベニーナ"),
            ("319", "ゆず"),
            ("319", "紅芯大根"),
            ("72", "春菊"),
            ("119", "パオ"),
            ("94", "紫キャベツ"),
            ("319", "すだち"),
            ("319", "仙台麩"),
            ("73", "雪菜"),
            ("156", "ししとう"),
            ("365", "切干大根"),
            ("319", "かぼす"),
            ("1334", "仙台麩カット"),
            ("73", "ちぢみ雪菜"),
            ("303", "にんにくの芽"),
            ("368", "凍り豆腐"),
            ("319", "ハス芽"),
            ("1334", "ズッキーニ"),
            ("123", "雪菜の花"),
            ("150", "トマト"),
            ("369", "もやし"),
            ("319", "ミニキャロット"),
            ("1334", "青ネギ"),
            ("78", "小松菜"),
            ("171", "ミニトマト"),
            ("369", "豆もやし"),
            ("319", "とんぶり"),
            ("1334", "カット野菜"),
            ("79", "山東菜"),
            ("117", "つぼみ菜"),
            ("345", "岡ヒジキ"),
            ("319", "エンダイブ"),
            ("856", "野菜MIX系"),
            ("81", "レタス"),
            ("92", "サラダ菜"),
            ("890", "油揚げ"),
            ("319", "ラディッシュ"),
            ("4502", "立桂"),
            ("47", "グリーンカール"),
            ("891", "豆腐"),
            ("319", "芽キャベツ"),
            ("83", "サニーレタス"),
            ("892", "蒟蒻"),
            ("319", "ハーブ系"),
            ("95", "セロリ"),
            ("893", "納豆"),
            ("319", "パプリカ"),
            ("90", "ブロッコリー"),
            ("373", "筍水煮"),
            ("319", "冬瓜"),
            ("85", "カリフラワー"),
            ("375", "ふき水煮"),
            ("319", "ゴーヤ"),
            ("98", "パセリ"),
            ("377", "ぜんまい水煮"),
            ("319", "モロッコいんげん"),
            ("116", "モロヘイヤ"),
            ("379", "蓮根水煮"),
            ("319", "絹さや"),
            ("111", "つる菜"),
            ("319", "大和芋"),
            ("319", "スナップエンドウ"),
            ("121", "チンゲン菜"),
            ("319", "エシャロット"),
            ("319", "グリーンピース"),
            ("319", "わさび"),
            ("319", "そら豆"),
        ],
    },
    {
        "departments": ["3課（照井）", "11課（相澤 史）"],
        "items": [
            ("21", "ごぼう"),
            ("360", "梅"),
            ("89", "アスパラガス"),
            ("23", "切ごぼうMIX"),
            ("360", "赤しそ"),
            ("169", "赤パプリカ"),
            ("26", "蓮根"),
            ("386", "梅干し"),
            ("170", "黄パプリカ"),
            ("60", "ふき"),
            ("386", "黒にんにく"),
            ("161", "オクラ"),
            ("225", "にんにく"),
            ("270", "花茗荷"),
            ("227", "らっきょう"),
            ("250", "生姜"),
            ("230", "ゆり根"),
            ("214", "洗い里芋"),
            ("218", "長いも"),
            ("901", "卵"),
            ("218", "山の芋"),
            ("902", "うずらの卵"),
            ("1", "大根"),
            ("339", "ヤングコーン"),
            ("14", "人参"),
            ("339", "山伏茸"),
            ("24", "筍"),
            ("321", "生椎茸"),
            ("41", "白菜"),
            ("322", "なめこ"),
            ("42", "キャベツ"),
            ("323", "えのき"),
            ("51", "長ネギ"),
            ("330", "しめじ"),
            ("53", "曲がりネギ"),
            ("324", "平茸"),
            ("136", "南瓜"),
            ("324", "あわび茸"),
            ("160", "とうもろこし"),
            ("325", "マッシュルーム"),
            ("201", "さつま芋"),
            ("327", "舞茸"),
            ("208", "じゃが芋"),
            ("1193", "エリンギ"),
            ("210", "メークイン"),
            ("326", "松茸"),
            ("221", "玉葱"),
            ("213", "里芋"),
            ("213", "芋ガラ"),
        ],
    },
    {
        "departments": ["5課", "12課（小林・牧野）"],
        "items": [
            ("400", "甘夏"),
            ("637", "貴陽"),
            ("692", "レッドグローブ"),
            ("415", "八朔"),
            ("604", "桃"),
            ("761", "バナナ"),
            ("418", "伊予柑"),
            ("632", "佐藤錦"),
            ("764", "パイナップル"),
            ("420", "デコポン"),
            ("649", "おうとう"),
            ("765", "パパイヤ"),
            ("452", "金柑"),
            ("660", "デラウェア"),
            ("767", "キウイフルーツ"),
            ("452", "ポンカン"),
            ("666", "キャンベル"),
            ("768", "アボカド"),
            ("452", "マーコット"),
            ("667", "シャインマスカット"),
            ("772", "ブルーベリー"),
            ("452", "ジューシーオレンジ"),
            ("669", "巨峰"),
            ("772", "ライム"),
            ("452", "みかん"),
            ("670", "ナイヤガラ"),
            ("776", "甘栗"),
            ("481", "三角みかん"),
            ("674", "ピオーネ"),
            ("773", "干し芋"),
            ("463", "味丸みかん"),
            ("681", "スチューベン"),
            ("780", "落花生"),
            ("519", "富士りんご"),
            ("704", "苺"),
            ("796", "ナッツ類"),
            ("510", "つがる"),
            ("741", "西瓜"),
            ("542", "幸水"),
            ("714", "アールスメロン"),
            ("544", "豊水"),
            ("723", "青肉メロン"),
            ("545", "新高梨"),
            ("731", "赤肉メロン"),
            ("564", "ラフランス"),
            ("766", "マンゴー"),
            ("571", "富有柿"),
            ("775", "無花果"),
            ("573", "平種柿"),
            ("1467", "果物セット"),
            ("573", "干し柿"),
            ("450", "グレープフルーツ"),
            ("573", "あんぽ柿"),
            ("450", "メロゴールド"),
            ("616", "大石"),
            ("450", "スウィーティー"),
            ("617", "ソルダム"),
            ("425", "レモン"),
            ("613", "太陽"),
            ("427", "オレンジ"),
        ],
    },
]


def item_options_for_category(category: str):
    items = ITEM_MASTER.get(category, [])
    return [f"{code} : {name}" for code, name in items]


@st.cache_data(ttl=3600)
def load_item_master_cached():
    master_dict = OrderedDict()
    item_flat_list = []
    rows = []
    for group in BUILTIN_ITEM_GROUPS:
        for dept in group["departments"]:
            master_dict.setdefault(dept, [])
            for code, name in group["items"]:
                master_dict[dept].append((code, name))
                item_flat_list.append(f"[{dept}] {code} : {name}")
                rows.append({"category": dept, "item_code": code, "item_name": name})
    return master_dict, pd.DataFrame(rows), item_flat_list

ITEM_MASTER, df_master_raw, ITEM_FLAT_LIST = load_item_master_cached()
DEFAULT_CATEGORY = next(iter(ITEM_MASTER.keys()), "1課（熊谷）")

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

# 【連続入力用の前回値キープ】
if "last_input_market_no" not in st.session_state:
    st.session_state.last_input_market_no = "M-001 (築地第一取引先)"

if "last_input_date" not in st.session_state:
    st.session_state.last_input_date = datetime.now().date()

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
        <p>連続入力高速化・PostgreSQLクラウド同期・耐スリープBCP自動バックアップ・社員別個別入力</p>
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
    "📥 CSV一括インポート"
]
tabs = st.tabs(tab_titles)

# ==========================================
# タブ 1: 取引データ入力・管理（超高速・連続入力フォーム対応）
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
                        "カテゴリー": exp_row.get("カテゴリー", "野菜"),
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
    # 📁 A. 取引先ファイルの自動記録
    # -------------------------------------------------------------
    with st.expander("📁 取引先CSV/TXTファイルから一括自動記録", expanded=False):
        col_up1, col_up2 = st.columns([4, 1])
        with col_up1:
            uploaded_entry_file = st.file_uploader(
                "取引データファイルを選択", 
                type=["csv", "txt", "tsv"], 
                key="auto_unique_verified_uploader", 
                label_visibility="collapsed"
            )
        with col_up2:
            if st.button("🧪 テストデータ3件", use_container_width=True):
                sample_items = [
                    {"日付": str(datetime.now().date()), "市場番号": "M-001 (築地第一取引先)", "カテゴリー": "1課（熊谷）", "商品コード": "150", "商品名": "トマト", "単価": 290, "数量": 50, "小計": 14500},
                    {"日付": str(datetime.now().date()), "市場番号": "M-001 (築地第一取引先)", "カテゴリー": "3課（照井）", "商品コード": "42", "商品名": "キャベツ", "単価": 200, "数量": 40, "小計": 8000},
                    {"日付": str(datetime.now().date()), "市場番号": "M-001 (築地第一取引先)", "カテゴリー": "5課", "商品コード": "519", "商品名": "富士りんご", "単価": 360, "数量": 30, "小計": 10800}
                ]
                insert_records_to_db(current_user, sample_items)
                st.session_state.verification_batch = {
                    "file_name": "サンプルデータ",
                    "df": pd.DataFrame(sample_items)
                }
                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.toast(f"✅ テストデータ 3件 を記録しました！")
                st.rerun()

        if uploaded_entry_file is not None:
            file_bytes = uploaded_entry_file.getvalue()
            file_hash = hashlib.md5(file_bytes).hexdigest()
            
            if file_hash not in st.session_state.processed_file_hashes:
                try:
                    st.session_state.processed_file_hashes.add(file_hash)
                    df_file = pd.read_csv(io.BytesIO(file_bytes))
                    
                    rename_map = {}
                    for col in df_file.columns:
                        c = str(col).strip()
                        if "日" in c: rename_map[col] = "日付"
                        elif "市場" in c or "取引先" in c: rename_map[col] = "市場番号"
                        elif "カテゴリ" in c: rename_map[col] = "カテゴリー"
                        elif "コード" in c: rename_map[col] = "商品コード"
                        elif "名" in c or "品" in c: rename_map[col] = "商品名"
                        elif "単価" in c: rename_map[col] = "単価"
                        elif "数" in c: rename_map[col] = "数量"
                        elif "計" in c or "金額" in c: rename_map[col] = "小計"
                    
                    df_file = df_file.rename(columns=rename_map)
                    if "日付" not in df_file.columns: df_file["日付"] = str(datetime.now().date())
                    if "市場番号" not in df_file.columns: df_file["市場番号"] = "M-001"
                    if "カテゴリー" not in df_file.columns: df_file["カテゴリー"] = DEFAULT_CATEGORY
                    if "商品コード" not in df_file.columns: df_file["商品コード"] = "AUTO"
                    if "単価" in df_file.columns and "数量" in df_file.columns:
                        df_file["小計"] = df_file["単価"].fillna(0) * df_file["数量"].fillna(0)

                    new_records_list = df_file.to_dict("records")
                    if new_records_list:
                        insert_records_to_db(current_user, new_records_list)
                        st.session_state.verification_batch = {
                            "file_name": uploaded_entry_file.name,
                            "df": df_file
                        }
                        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                        st.toast(f"✅ 『{uploaded_entry_file.name}』から {len(new_records_list)} 件を自動記録しました！")
                        st.rerun()
                except Exception as e:
                    st.error(f"ファイル解析エラー: {e}")

    # -------------------------------------------------------------
    # ⚡ B. 手動記録（カテゴリー＝○課 / 商品＝選択した課のコード表）
    # -------------------------------------------------------------
    st.markdown("""
        <div style="background-color: #f0fdf4; border: 1.5px solid #86efac; border-radius: 8px; padding: 10px 14px; margin-bottom: 8px;">
            <b style="color: #166534;">📝 手動記録（課別コード表連動）:</b>
            <span style="color: #4b5563; font-size: 13px;">
                カテゴリーで課を選ぶと、商品欄にはその課のコード表に載っている品名だけが表示されます。
            </span>
        </div>
    """, unsafe_allow_html=True)

    col_top1, col_top2 = st.columns([2, 1])
    with col_top1:
        form_market_no = st.text_input(
            "市場番号 / 取引先名 (連続入力中は記憶されます)",
            value=st.session_state.last_input_market_no,
            key="speed_form_market"
        )
    with col_top2:
        form_input_date = st.date_input(
            "取引日付",
            value=st.session_state.last_input_date,
            key="speed_form_date"
        )

    st.caption("▼ 商品行（カテゴリーで課を選び、該当商品にチェックを入れて記録してください）")

    form_rows_data = []
    all_categories = list(ITEM_MASTER.keys()) or [DEFAULT_CATEGORY]

    header_chk, header_cat, header_item, header_price, header_qty = st.columns([0.8, 2.2, 3.2, 1.9, 1.9])
    header_chk.markdown("**行**")
    header_cat.markdown("**カテゴリー（課）**")
    header_item.markdown("**商品（選択した課の品目）**")
    header_price.markdown("**単価**")
    header_qty.markdown("**数量**")

    for i in range(1, 6):
        c_chk, c_cat, c_item, c_price, c_qty = st.columns([0.8, 2.2, 3.2, 1.9, 1.9])
        with c_chk:
            is_active = st.checkbox(f"行 {i}", value=(i == 1), key=f"f_active_{i}")
        with c_cat:
            cat_val = st.selectbox(
                f"カテゴリー（課） #{i}",
                all_categories,
                index=(i - 1) % len(all_categories),
                key=f"f_cat_{i}",
                label_visibility="collapsed"
            )

        item_options = item_options_for_category(cat_val)
        if not item_options:
            item_options = ["（この課の商品マスタがありません）"]

        with c_item:
            item_val = st.selectbox(
                f"商品 #{i}",
                item_options,
                index=0,
                key=f"f_item_{i}_{cat_val}",
                label_visibility="collapsed"
            )
        with c_price:
            price_val = st.number_input(
                f"単価 #{i}",
                min_value=0,
                value=150 if is_active else 0,
                step=10,
                key=f"f_price_{i}",
                label_visibility="collapsed"
            )
        with c_qty:
            qty_val = st.number_input(
                f"数量 #{i}",
                min_value=0,
                value=10 if is_active else 0,
                step=1,
                key=f"f_qty_{i}",
                label_visibility="collapsed"
            )

        form_rows_data.append({
            "active": is_active,
            "category": cat_val,
            "item_str": item_val,
            "price": price_val,
            "qty": qty_val
        })

    col_sub1, col_sub2 = st.columns([3, 1])
    with col_sub1:
        submit_manual_batch = st.button(
            "➕ 入力した商品を手動記録する",
            type="primary",
            use_container_width=True,
            key="manual_record_submit"
        )
    with col_sub2:
        st.caption("※ 課を切り替えると商品一覧が即時に変わります")

    if submit_manual_batch:
        if not form_market_no.strip():
            st.error("❌ 市場番号を入力してください。")
        else:
            st.session_state.last_input_market_no = form_market_no
            st.session_state.last_input_date = form_input_date

            valid_new_items = []
            for r in form_rows_data:
                if r["active"] and r["item_str"] and " : " in r["item_str"] and r["price"] > 0 and r["qty"] > 0:
                    parts = r["item_str"].split(" : ", 1)
                    code = parts[0]
                    name = parts[1] if len(parts) > 1 else parts[0]
                    valid_new_items.append({
                        "日付": str(form_input_date),
                        "市場番号": form_market_no,
                        "カテゴリー": r["category"],
                        "商品コード": code,
                        "商品名": name,
                        "単価": r["price"],
                        "数量": r["qty"],
                        "小計": r["price"] * r["qty"]
                    })

            if valid_new_items:
                insert_records_to_db(current_user, valid_new_items, auto_backup=False)
                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.toast(f"⚡ {len(valid_new_items)} 件を手動記録しました！続けて次のデータを入力できます。")
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
            "カテゴリー": st.column_config.SelectboxColumn("カテゴリー（課）", options=list(ITEM_MASTER.keys()) or [DEFAULT_CATEGORY]),
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
# タブ 5: CSV一括インポート
# ==========================================
with tabs[4]:
    st.markdown('<div class="section-title">📥 CSV一括インポート</div>', unsafe_allow_html=True)
    uploaded_file = st.file_uploader("取引データCSVを選択", type=["csv"], key="bulk_csv_uploader")
    if uploaded_file is not None:
        try:
            df_up = pd.read_csv(uploaded_file)
            req_cols = ["日付", "市場番号", "カテゴリー", "商品コード", "商品名", "単価", "数量", "小計"]
            if all(col in df_up.columns for col in req_cols):
                st.dataframe(df_up.head())
                if st.button("📥 インポート実行", type="primary"):
                    insert_records_to_db(current_user, df_up.to_dict("records"))
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.success(f"インポート完了しました！（登録者: {current_user}）")
                    st.rerun()
        except Exception as e:
            st.error(f"エラー: {e}")
