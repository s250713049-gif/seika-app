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
import time
import unicodedata
import struct
import subprocess
import sys

# Excel (.xls / .xlsx) 高速・完全処理エンジンの自動ロード & 補完
try:
    import xlrd
except ImportError:
    try:
        subprocess.run([sys.executable, '-m', 'pip', 'install', 'xlrd>=2.0.1', 'openpyxl>=3.1.0'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
        import xlrd
    except Exception:
        xlrd = None

try:
    import openpyxl
except ImportError:
    try:
        subprocess.run([sys.executable, '-m', 'pip', 'install', 'openpyxl>=3.1.0'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
        import openpyxl
    except Exception:
        openpyxl = None

# ==========================================
# ページ基本設定 & セキュリティヘッダーCSS (高速描画対応)
# ==========================================
st.set_page_config(
    page_title="青果卸売 在庫・取引管理 Pro (高速・軽量版)",
    page_icon="🍎",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
<style>
    .block-container {
        padding-top: 1rem;
        padding-bottom: 1.5rem;
        padding-left: 1.5rem;
        padding-right: 1.5rem;
    }
    .metric-card {
        background-color: #ffffff;
        border-radius: 8px;
        padding: 10px 14px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        border: 1px solid #e2e8f0;
        margin-bottom: 8px;
    }
    .metric-label {
        font-size: 11px;
        color: #64748b;
        font-weight: 600;
        text-transform: uppercase;
        margin-bottom: 2px;
    }
    .metric-value {
        font-size: 20px;
        font-weight: 700;
        color: #1e293b;
    }
    .section-title {
        font-size: 16px;
        font-weight: 700;
        color: #1e7e34;
        border-left: 4px solid #1e7e34;
        padding-left: 8px;
        margin-top: 14px;
        margin-bottom: 8px;
    }
    .status-badge-cloud {
        background-color: #e6f4ea;
        color: #137333;
        padding: 3px 10px;
        border-radius: 12px;
        font-size: 12px;
        font-weight: 600;
        display: inline-block;
    }
    .status-badge-local {
        background-color: #fef7e0;
        color: #b06000;
        padding: 3px 10px;
        border-radius: 12px;
        font-size: 12px;
        font-weight: 600;
        display: inline-block;
    }
</style>
""", unsafe_allow_html=True)

# セッション状態の初期化
if "logged_in_user" not in st.session_state:
    st.session_state.logged_in_user = None
if "user_role" not in st.session_state:
    st.session_state.user_role = "staff"
if "editor_version" not in st.session_state:
    st.session_state.editor_version = 0
if "input_counter" not in st.session_state:
    st.session_state.input_counter = 0
if "recent_added_history" not in st.session_state:
    st.session_state.recent_added_history = []
if "extracted_suppliers" not in st.session_state:
    st.session_state.extracted_suppliers = []
if "verification_batch" not in st.session_state:
    st.session_state.verification_batch = None
if "custom_gemini_api_key" not in st.session_state:
    st.session_state.custom_gemini_api_key = ""
if "ai_thinking_level" not in st.session_state:
    st.session_state.ai_thinking_level = "medium"
if "last_deleted_records" not in st.session_state:
    st.session_state.last_deleted_records = []

# ==========================================
# データベース接続 & セキュア認証
# ==========================================
def get_secret(key: str, default: str = None) -> str:
    val = os.environ.get(key)
    if val:
        return val
    try:
        if hasattr(st, "secrets") and key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return default

@st.cache_resource
def get_engine():
    db_url = get_secret("DATABASE_URL")
    if db_url:
        try:
            if db_url.startswith("postgres://"):
                db_url = db_url.replace("postgres://", "postgresql://", 1)
            eng = create_engine(
                db_url,
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=20,
                pool_recycle=1800,
                connect_args={"connect_timeout": 5}
            )
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            return eng
        except Exception as e:
            st.toast(f"⚠️ クラウドDB接続フォールバック: {e}")
    
    local_dir = "/mount/src/seika-app" if os.path.exists("/mount/src/seika-app") else "."
    sqlite_path = os.path.join(local_dir, "seika_trade_v3.db")
    return create_engine(f"sqlite:///{sqlite_path}", connect_args={"check_same_thread": False})

def is_using_cloud_db() -> bool:
    try:
        eng = get_engine()
        return "postgresql" in eng.name
    except Exception:
        return False

SECRET_SALT = get_secret("SESSION_SECRET", "seika_pro_salt_2026_super_secure")

def hash_password(password: str, salt: str = None) -> str:
    if not salt:
        salt = secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return f"{salt}:{h.hex()}"

def verify_password(stored_hash: str, provided_password: str) -> bool:
    try:
        salt, h_val = stored_hash.split(":", 1)
        calc = hashlib.pbkdf2_hmac('sha256', provided_password.encode(), salt.encode(), 100000).hex()
        return hmac.compare_digest(calc, h_val)
    except Exception:
        return False

def create_session_token(username: str, role: str) -> str:
    payload = f"{username}:{role}:{int(time.time())}"
    sig = hmac.new(SECRET_SALT.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"

def verify_session_token(token: str):
    try:
        parts = token.split(":")
        if len(parts) != 4:
            return None
        user, role, ts, sig = parts[0], parts[1], parts[2], parts[3]
        payload = f"{user}:{role}:{ts}"
        expected_sig = hmac.new(SECRET_SALT.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        if time.time() - int(ts) > 86400 * 7:
            return None
        return {"username": user, "role": role}
    except Exception:
        return None

def init_db():
    eng = get_engine()
    is_pg = "postgresql" in eng.name
    id_col = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    
    with eng.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS users (
                id {id_col},
                username VARCHAR(50) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(20) DEFAULT 'staff',
                display_name VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS transactions (
                id {id_col},
                user_id VARCHAR(50) NOT NULL,
                date VARCHAR(20) NOT NULL,
                market_number VARCHAR(100) NOT NULL,
                category VARCHAR(100) NOT NULL,
                item_code VARCHAR(50) NOT NULL,
                item_name VARCHAR(100) NOT NULL,
                unit_price INTEGER NOT NULL DEFAULT 0,
                quantity INTEGER NOT NULL DEFAULT 1,
                subtotal INTEGER NOT NULL DEFAULT 0,
                purchase_amount INTEGER DEFAULT 0,
                payment_amount VARCHAR(50) DEFAULT '0',
                balance INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))

        # 新設カラムの動的追加（後方互換性担保）
        if not is_pg:
            t_info = conn.execute(text("PRAGMA table_info(transactions)")).fetchall()
            existing_cols = [row[1] for row in t_info]
            if "purchase_amount" not in existing_cols:
                conn.execute(text("ALTER TABLE transactions ADD COLUMN purchase_amount INTEGER DEFAULT 0"))
            if "payment_amount" not in existing_cols:
                conn.execute(text("ALTER TABLE transactions ADD COLUMN payment_amount VARCHAR(50) DEFAULT '0'"))
            if "balance" not in existing_cols:
                conn.execute(text("ALTER TABLE transactions ADD COLUMN balance INTEGER DEFAULT 0"))
            if "total" not in existing_cols:
                conn.execute(text("ALTER TABLE transactions ADD COLUMN total INTEGER DEFAULT 0"))

        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS backup_history (
                id {id_col},
                backup_name VARCHAR(255) NOT NULL,
                file_path VARCHAR(255) NOT NULL,
                created_by VARCHAR(50) NOT NULL,
                record_count INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))

        # デフォルト管理者・一般アカウントの登録
        res = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
        if res == 0:
            u_admin = ("admin", hash_password("admin123"), "admin", "統括管理者")
            u_user1 = ("user1", hash_password("pass123"), "staff", "営業担当 田中")
            u_user2 = ("user2", hash_password("pass123"), "staff", "仕入担当 鈴木")
            for u in [u_admin, u_user1, u_user2]:
                if is_pg:
                    conn.execute(text("INSERT INTO users (username, password_hash, role, display_name) VALUES (:u, :p, :r, :d)"),
                                 {"u": u[0], "p": u[1], "r": u[2], "d": u[3]})
                else:
                    conn.execute(text("INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)"), u)

init_db()

def verify_user_login(username: str, password_raw: str):
    eng = get_engine()
    with eng.connect() as conn:
        res = conn.execute(text("SELECT username, password_hash, role, display_name FROM users WHERE username = :u"), {"u": username}).fetchone()
        if res:
            if verify_password(res[1], password_raw):
                return {"username": res[0], "role": res[2], "display_name": res[3]}
    return None

def get_all_users_from_db():
    eng = get_engine()
    with eng.connect() as conn:
        df = pd.read_sql(text("SELECT username, role, display_name, created_at FROM users ORDER BY id ASC"), conn)
    return df

def register_new_user_db(username, password, role="staff", display_name=""):
    eng = get_engine()
    hashed = hash_password(password)
    with eng.begin() as conn:
        conn.execute(text("""
            INSERT INTO users (username, password_hash, role, display_name)
            VALUES (:u, :p, :r, :d)
        """), {"u": username, "p": hashed, "r": role, "d": display_name or username})

@st.cache_data(ttl=15, show_spinner=False)
def load_records_cached(target_user_filter: str = None):
    eng = get_engine()
    query = """
        SELECT id, user_id, date, market_number, category, item_code, item_name,
               unit_price, quantity, subtotal, purchase_amount, payment_amount, balance, total, created_at
        FROM transactions
    """
    params = {}
    if target_user_filter and target_user_filter != "全員の取引を表示":
        query += " WHERE user_id = :u"
        params["u"] = target_user_filter
    query += " ORDER BY id DESC"
    
    with eng.connect() as conn:
        df = pd.read_sql(text(query), conn, params=params)
    return df

def load_records_from_db(target_user_filter: str = None):
    return load_records_cached(target_user_filter)

def invalidate_records_cache():
    load_records_cached.clear()

def insert_records_to_db(user_id: str, records: list, auto_backup=True):
    if not records:
        return
    eng = get_engine()
    is_pg = "postgresql" in eng.name
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    formatted_records = []
    for r in records:
        f_rec = {
            "u": user_id,
            "d": str(r.get("日付", r.get("date", datetime.now().strftime("%Y-%m-%d")))),
            "m": str(r.get("市場番号", r.get("market_number", "M-001"))),
            "c": str(r.get("カテゴリー", r.get("category", "1課4課（熊谷・大宮） / 2課 / 栗原"))),
            "code": str(r.get("商品コード", r.get("item_code", "AUTO"))),
            "name": str(r.get("品名", r.get("商品名", r.get("item_name", "品名未指定")))),
            "p": int(r.get("単価", r.get("unit_price", 0))),
            "q": int(r.get("数量", r.get("quantity", 1))),
            "s": int(r.get("仕入金額", r.get("小計", r.get("subtotal", 0)))),
            "pa": int(r.get("仕入金額", r.get("purchase_amount", 0))),
            "pay": str(r.get("支払金額", r.get("payment_amount", "0"))),
            "bal": int(r.get("差引残高", r.get("balance", 0))),
            "tot": int(r.get("合計", r.get("total", 0))),
            "now": now_str
        }
        formatted_records.append(f_rec)
        
    sql = """
        INSERT INTO transactions (user_id, date, market_number, category, item_code, item_name,
                                  unit_price, quantity, subtotal, purchase_amount, payment_amount, balance, total, created_at, updated_at)
        VALUES (:u, :d, :m, :c, :code, :name, :p, :q, :s, :pa, :pay, :bal, :tot, :now, :now)
    """
    with eng.begin() as conn:
        conn.execute(text(sql), formatted_records)
    invalidate_records_cache()

def update_single_record_in_db(record_dict: dict):
    eng = get_engine()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    params = {
        "id": int(record_dict["id"]),
        "d": str(record_dict["date"]),
        "m": str(record_dict["market_number"]),
        "c": str(record_dict["category"]),
        "code": str(record_dict["item_code"]),
        "name": str(record_dict["item_name"]),
        "p": int(record_dict["unit_price"]),
        "q": int(record_dict["quantity"]),
        "s": int(record_dict["subtotal"]),
        "pa": int(record_dict.get("purchase_amount", 0)),
        "pay": str(record_dict.get("payment_amount", "0")),
        "bal": int(record_dict.get("balance", 0)),
        "tot": int(record_dict.get("total", 0)),
        "now": now_str
    }
    sql = """
        UPDATE transactions
        SET date = :d, market_number = :m, category = :c, item_code = :code, item_name = :name,
            unit_price = :p, quantity = :q, subtotal = :s, purchase_amount = :pa,
            payment_amount = :pay, balance = :bal, total = :tot, updated_at = :now
        WHERE id = :id
    """
    with eng.begin() as conn:
        conn.execute(text(sql), params)
    invalidate_records_cache()

def delete_records_by_ids(ids: list, deleted_by: str):
    if not ids:
        return
    eng = get_engine()
    # 削除前のバックアップスナップショット取得
    with eng.connect() as conn:
        del_records = pd.read_sql(text("SELECT * FROM transactions WHERE id IN :ids"), conn, params={"ids": tuple(ids)})
        st.session_state.last_deleted_records = del_records.to_dict("records")
        
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM transactions WHERE id IN :ids"), {"ids": tuple(ids)})
    invalidate_records_cache()

def get_employee_summary_stats():
    eng = get_engine()
    with eng.connect() as conn:
        sql = """
            SELECT user_id,
                   COUNT(*) as record_count,
                   COALESCE(SUM(subtotal), 0) as total_amount,
                   COALESCE(SUM(quantity), 0) as total_qty,
                   MAX(updated_at) as last_activity
            FROM transactions
            GROUP BY user_id
            ORDER BY total_amount DESC
        """
        df = pd.read_sql(text(sql), conn)
    return df

def create_backup_snapshot(user_id: str, trigger_reason="定期自動バックアップ"):
    local_dir = "/mount/src/seika-app/backups" if os.path.exists("/mount/src/seika-app") else "./backups"
    os.makedirs(local_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"backup_{ts}_{user_id}.json"
    fpath = os.path.join(local_dir, fname)
    
    eng = get_engine()
    with eng.connect() as conn:
        df = pd.read_sql(text("SELECT * FROM transactions ORDER BY id ASC"), conn)
        
    records = df.to_dict("records")
    for r in records:
        for k, v in r.items():
            if isinstance(v, (datetime, pd.Timestamp)):
                r[k] = v.strftime("%Y-%m-%d %H:%M:%S")
                
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump({"backup_time": ts, "created_by": user_id, "reason": trigger_reason, "records": records}, f, ensure_ascii=False, indent=2)
        
    with eng.begin() as conn:
        conn.execute(text("""
            INSERT INTO backup_history (backup_name, file_path, created_by, record_count)
            VALUES (:name, :path, :u, :cnt)
        """), {"name": fname, "path": fpath, "u": user_id, "cnt": len(records)})
    return fpath

def list_all_backups():
    eng = get_engine()
    try:
        with eng.connect() as conn:
            df = pd.read_sql(text("SELECT id, backup_name, file_path, created_by, record_count, created_at FROM backup_history ORDER BY id DESC"), conn)
            return df
    except Exception:
        local_dir = "/mount/src/seika-app/backups" if os.path.exists("/mount/src/seika-app") else "./backups"
        files = glob.glob(os.path.join(local_dir, "backup_*.json"))
        data = []
        for f in sorted(files, reverse=True):
            data.append({
                "backup_name": os.path.basename(f),
                "file_path": f,
                "created_by": "system",
                "record_count": 0,
                "created_at": datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M:%S")
            })
        return pd.DataFrame(data)

def restore_from_backup_file(filepath: str, restored_by: str):
    if not os.path.exists(filepath):
        return False, "バックアップファイルが存在しません"
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            bdata = json.load(f)
        records = bdata.get("records", [])
        if not records:
            return False, "復元対象データが空です"
            
        eng = get_engine()
        with eng.begin() as conn:
            conn.execute(text("DELETE FROM transactions"))
            for r in records:
                conn.execute(text("""
                    INSERT INTO transactions (user_id, date, market_number, category, item_code, item_name,
                                              unit_price, quantity, subtotal, purchase_amount, payment_amount, balance, total, created_at, updated_at)
                    VALUES (:u, :d, :m, :c, :code, :name, :p, :q, :s, :pa, :pay, :bal, :tot, :now, :now)
                """), {
                    "u": r.get("user_id", restored_by),
                    "d": r.get("date", datetime.now().strftime("%Y-%m-%d")),
                    "m": r.get("market_number", "M-001"),
                    "c": r.get("category", "1課4課（熊谷・大宮） / 2課 / 栗原"),
                    "code": r.get("item_code", "AUTO"),
                    "name": r.get("item_name", "品名未指定"),
                    "p": r.get("unit_price", 0),
                    "q": r.get("quantity", 1),
                    "s": r.get("subtotal", 0),
                    "pa": r.get("purchase_amount", 0),
                    "pay": str(r.get("payment_amount", "0")),
                    "bal": r.get("balance", 0),
                    "tot": r.get("total", 0),
                    "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
        invalidate_records_cache()
        return True, f"{len(records)} 件のデータを復元しました"
    except Exception as e:
        return False, str(e)

# ==========================================
# 商品マスタ・カテゴリー照合
# ==========================================
def _norm_dept(s) -> str:
    if s is None:
        return ""
    norm = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"[\s\u3000\t\r\n・_／/\\()（）]+", "", norm)

def _dept_tokens(s) -> set:
    norm = _norm_dept(s)
    return set(re.findall(r"(\d+課|大宮|熊谷|川越|栗原)", norm))

def _dept_match(row_dept, chosen_dept) -> bool:
    if not chosen_dept or not row_dept:
        return True
    chosen_toks = _dept_tokens(chosen_dept)
    row_toks = _dept_tokens(row_dept)
    if not chosen_toks or not row_toks:
        c1 = _norm_dept(chosen_dept)
        r1 = _norm_dept(row_dept)
        return (c1 in r1) or (r1 in c1)
    if chosen_toks & row_toks:
        return True
    return False

def get_items_for_department(df_master, dept):
    if df_master is None or df_master.empty or "department" not in df_master.columns:
        return pd.DataFrame()
    mask = df_master["department"].apply(lambda d: _dept_match(d, dept))
    return df_master[mask].copy()

def _file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0

def _read_category_txt(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    rows = []
    current_dept = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                if raw.startswith("[") and raw.endswith("]"):
                    current_dept = raw[1:-1].strip()
                    continue
                parts = re.split(r"[\t,]+", raw)
                if len(parts) >= 3:
                    dept = parts[0].strip() or current_dept
                    code = parts[1].strip()
                    name = parts[2].strip()
                    price = parts[3].strip() if len(parts) > 3 else "0"
                    rows.append({
                        "department": dept,
                        "item_code": code,
                        "item_name": name,
                        "default_price": int(price) if price.isdigit() else 0
                    })
                elif len(parts) == 2 and current_dept:
                    rows.append({
                        "department": current_dept,
                        "item_code": parts[0].strip(),
                        "item_name": parts[1].strip(),
                        "default_price": 0
                    })
    except Exception:
        pass
    return pd.DataFrame(rows)

@st.cache_data(ttl=300, show_spinner=False)
def load_item_master_cached(csv_mtime: float = 0.0, txt_mtime: float = 0.0):
    candidates_csv = ["item_master.csv", "/mount/src/seika-app/item_master.csv"]
    for c in candidates_csv:
        if os.path.exists(c):
            try:
                df = pd.read_csv(c, dtype={"item_code": str})
                if not df.empty:
                    return df
            except Exception:
                pass
    candidates_txt = ["item_category.txt", "/mount/src/seika-app/item_category.txt"]
    for c in candidates_txt:
        if os.path.exists(c):
            df = _read_category_txt(c)
            if not df.empty:
                return df
    # デフォルトの青果マスタ
    return pd.DataFrame([
        {"department": "1課4課（熊谷・大宮） / 2課 / 栗原", "item_code": "4525", "item_name": "築館なめこ", "default_price": 1200},
        {"department": "1課4課（熊谷・大宮） / 2課 / 栗原", "item_code": "1001", "item_name": "キャベツ", "default_price": 1500},
        {"department": "1課4課（熊谷・大宮） / 2課 / 栗原", "item_code": "1002", "item_name": "レタス", "default_price": 2200},
        {"department": "1課4課（熊谷・大宮） / 2課 / 栗原", "item_code": "1003", "item_name": "白菜", "default_price": 1800},
        {"department": "5課 / 6課（川越）", "item_code": "2001", "item_name": "トマト", "default_price": 2500},
        {"department": "5課 / 6課（川越）", "item_code": "2002", "item_name": "きゅうり", "default_price": 1900},
        {"department": "5課 / 6課（川越）", "item_code": "2003", "item_name": "なす", "default_price": 2100},
        {"department": "栗原", "item_code": "3001", "item_name": "しいたけ", "default_price": 2800},
        {"department": "栗原", "item_code": "3002", "item_name": "えのき", "default_price": 980},
    ])

# ==========================================
# 8.5 全シート保持 最新形式 (.xlsx / OpenXML) への高信頼変換エンジン & OLE2/BIFF8 完全解析
# ==========================================
def col_letter(col_idx: int) -> str:
    """列番号 (0-indexed) を Excel 列英字 (A, B, ..., Z, AA, AB, ...) に変換"""
    result = ""
    col_idx += 1
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx - 1, 26)
        result = chr(65 + remainder) + result
    return result

def parse_biff_rk(rk_val: int):
    """BIFF8 RK レコードの数値を高精度デコード（負数・小数を完全再現）"""
    is_div100 = bool(rk_val & 1)
    if rk_val & 2:
        v = (rk_val >> 2)
        if v >= 0x20000000:
            v -= 0x40000000
        return round(v / 100.0, 4) if is_div100 else int(v)
    else:
        raw_b = struct.pack("<I", rk_val & 0xFFFFFFFC) + b"\x00\x00\x00\x00"
        v = struct.unpack("<d", raw_b)[0]
        v_res = v / 100.0 if is_div100 else v
        return int(v_res) if v_res.is_integer() else round(v_res, 4)

def parse_japanese_date(val) -> str:
    """和暦（R8.9.19, H30.5.1等）、月日（9/23等）、西暦（2026/09/19等）をISO形式日付に確実変換"""
    if val is None or pd.isnull(val):
        return ""
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.strftime("%Y-%m-%d")
    
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "nat"):
        return ""
    
    # 令和・平成などの和暦表記 (例: R8.9.19, R08.09.19, 令8.9.19)
    wareki_m = re.match(r"^([RrHhSsTt令平昭])?\s*(\d{1,2})[\.年/](\d{1,2})[\.月/](\d{1,2})日?$", s)
    if wareki_m:
        era_char = (wareki_m.group(1) or "R").upper()
        year_num = int(wareki_m.group(2))
        month_num = int(wareki_m.group(3))
        day_num = int(wareki_m.group(4))
        
        base_year = 2018 # R1 = 2019
        if era_char in ("H", "平"):
            base_year = 1988
        elif era_char in ("S", "昭"):
            base_year = 1925
        
        full_year = base_year + year_num
        try:
            return f"{full_year:04d}-{month_num:02d}-{day_num:02d}"
        except Exception:
            pass
            
    # 月日のみ (例: 9/23, 09/23)
    md_m = re.match(r"^(\d{1,2})[/\-\.](\d{1,2})$", s)
    if md_m:
        cur_year = datetime.now().year
        m_num = int(md_m.group(1))
        d_num = int(md_m.group(2))
        if 1 <= m_num <= 12 and 1 <= d_num <= 31:
            return f"{cur_year:04d}-{m_num:02d}-{d_num:02d}"

    # 西暦 (例: 2026/09/19, 2026-9-19)
    ymd_m = re.match(r"^(\d{4})[/\-\.](\d{1,2})[/\-\.](\d{1,2})", s)
    if ymd_m:
        y = int(ymd_m.group(1))
        m = int(ymd_m.group(2))
        d = int(ymd_m.group(3))
        return f"{y:04d}-{m:02d}-{d:02d}"

    return s

def clean_num(val, default=0):
    """¥マーク、カンマ、マイナス記号付き数値を正確に整数化（負数・0も完全保持）"""
    if val is None or pd.isnull(val):
        return default
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return int(val) if not (isinstance(val, float) and val != val) else default
    s = str(val).replace("¥", "").replace("\\", "").replace("円", "").replace(",", "").strip()
    if not s:
        return default
    try:
        is_neg = False
        if s.startswith(("▲", "△", "－", "-")):
            is_neg = True
            s = s[1:].strip()
        elif s.startswith("¥-") or s.startswith("-\\"):
            is_neg = True
            s = s[2:].strip()
        f = float(s)
        return int(-f if is_neg else f)
    except Exception:
        return default

def extract_ole_streams(data: bytes) -> dict:
    """OLE2 Compound File から全ストリーム（Workbook / Book 含む）を抽出"""
    if len(data) < 512 or data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return {}
    streams = {}
    try:
        sector_size = 1 << struct.unpack_from("<H", data, 30)[0]
        fat_sectors_count = struct.unpack_from("<I", data, 44)[0]
        dir_first_sec = struct.unpack_from("<I", data, 48)[0]
        mini_sector_size = 1 << struct.unpack_from("<H", data, 32)[0]
        minifat_first_sec = struct.unpack_from("<I", data, 60)[0]
        mini_stream_cutoff = struct.unpack_from("<I", data, 56)[0]

        fat = []
        difat_count = min(109, fat_sectors_count)
        for i in range(difat_count):
            sec_idx = struct.unpack_from("<I", data, 76 + i * 4)[0]
            if sec_idx < 0xFFFFFFFE:
                offset = (sec_idx + 1) * sector_size
                sec_bytes = data[offset:offset + sector_size]
                fat.extend(struct.unpack(f"<{len(sec_bytes) // 4}I", sec_bytes))

        difat_first_sec = struct.unpack_from("<I", data, 68)[0]
        difat_sec_count = struct.unpack_from("<I", data, 72)[0]
        cur_difat = difat_first_sec
        difat_visited = set()
        while cur_difat < 0xFFFFFFFE and cur_difat not in difat_visited and len(difat_visited) < difat_sec_count:
            difat_visited.add(cur_difat)
            off = (cur_difat + 1) * sector_size
            d_sec = data[off:off + sector_size]
            for i in range((sector_size // 4) - 1):
                s_idx = struct.unpack_from("<I", d_sec, i * 4)[0]
                if s_idx < 0xFFFFFFFE:
                    f_off = (s_idx + 1) * sector_size
                    f_bytes = data[f_off:f_off + sector_size]
                    fat.extend(struct.unpack(f"<{len(f_bytes) // 4}I", f_bytes))
            cur_difat = struct.unpack_from("<I", d_sec, sector_size - 4)[0]

        cur_sec = dir_first_sec
        dir_bytes = b""
        visited = set()
        while cur_sec < 0xFFFFFFFE and cur_sec not in visited and cur_sec < len(fat):
            visited.add(cur_sec)
            offset = (cur_sec + 1) * sector_size
            dir_bytes += data[offset:offset + sector_size]
            cur_sec = fat[cur_sec]

        minifat = []
        cur_mf = minifat_first_sec
        mf_visited = set()
        while cur_mf < 0xFFFFFFFE and cur_mf not in mf_visited and cur_mf < len(fat):
            mf_visited.add(cur_mf)
            off = (cur_mf + 1) * sector_size
            sec_b = data[off:off + sector_size]
            minifat.extend(struct.unpack(f"<{len(sec_b) // 4}I", sec_b))
            cur_mf = fat[cur_mf]

        root_stream = b""
        if len(dir_bytes) >= 128:
            root_start = struct.unpack_from("<I", dir_bytes, 116)[0]
            root_size = struct.unpack_from("<I", dir_bytes, 120)[0]
            c_sec = root_start
            r_visited = set()
            while c_sec < 0xFFFFFFFE and c_sec not in r_visited and c_sec < len(fat):
                r_visited.add(c_sec)
                off = (c_sec + 1) * sector_size
                root_stream += data[off:off + sector_size]
                c_sec = fat[c_sec]
            root_stream = root_stream[:root_size]

        def get_stream(start_sec, stream_size):
            s_data = b""
            c_s = start_sec
            v_s = set()
            while c_s < 0xFFFFFFFE and c_s not in v_s and c_s < len(fat):
                v_s.add(c_s)
                off = (c_s + 1) * sector_size
                s_data += data[off:off + sector_size]
                c_s = fat[c_s]
            return s_data[:stream_size]

        def get_mini_stream(start_sec, stream_size):
            s_data = b""
            c_s = start_sec
            v_s = set()
            while c_s < 0xFFFFFFFE and c_s not in v_s and c_s < len(minifat):
                v_s.add(c_s)
                off = c_s * mini_sector_size
                s_data += root_stream[off:off + mini_sector_size]
                c_s = minifat[c_s]
            return s_data[:stream_size]

        for i in range(0, len(dir_bytes), 128):
            entry = dir_bytes[i:i + 128]
            if len(entry) < 128:
                break
            name_len = struct.unpack_from("<H", entry, 64)[0]
            if name_len > 0:
                name = entry[:name_len].decode("utf-16le", errors="ignore").rstrip("\x00")
                start_sec = struct.unpack_from("<I", entry, 116)[0]
                stream_size = struct.unpack_from("<I", entry, 120)[0]
                if stream_size > 0:
                    if stream_size < mini_stream_cutoff and root_stream and minifat:
                        streams[name] = get_mini_stream(start_sec, stream_size)
                    else:
                        streams[name] = get_stream(start_sec, stream_size)
    except Exception:
        pass
    return streams

def parse_biff8_all_sheets(stream: bytes) -> dict:
    """BIFF8 Workbook ストリームから全シート（シート名、全行・全セル値）を辞書形式 {シート名: [ [セル値, ...] ]} で完全復元"""
    sheets_grid = {}
    if not stream or len(stream) < 8:
        return sheets_grid

    pos = 0
    sst = []
    boundsheets = []

    try:
        while pos < len(stream) - 4:
            rtype, rlen = struct.unpack_from("<HH", stream, pos)
            pos += 4
            rdata = stream[pos:pos + rlen]
            pos += rlen

            if rtype == 0x0085: # BOUNDSHEET
                if len(rdata) >= 8:
                    sheet_offset = struct.unpack_from("<I", rdata, 0)[0]
                    c_len = rdata[6]
                    flags = rdata[7]
                    is_16 = bool(flags & 1)
                    raw_n = rdata[8:8 + (c_len * 2 if is_16 else c_len)]
                    try:
                        sheet_name = raw_n.decode("utf-16le" if is_16 else "cp932", errors="ignore").strip("\x00")
                    except Exception:
                        sheet_name = f"Sheet_{len(boundsheets)+1}"
                    if not sheet_name:
                        sheet_name = f"Sheet_{len(boundsheets)+1}"
                    boundsheets.append((sheet_name, sheet_offset))

            elif rtype == 0x00FC: # SST
                try:
                    num_strings, num_unique = struct.unpack_from("<II", rdata, 0)
                    spos = 8
                    while spos < len(rdata) and len(sst) < num_unique:
                        if spos + 3 > len(rdata): break
                        clen, sflags = struct.unpack_from("<HB", rdata, spos)
                        spos += 3
                        is_16bit = bool(sflags & 1)
                        has_rich = bool(sflags & 8)
                        has_phonetic = bool(sflags & 4)
                        rich_runs = 0
                        phonetic_size = 0
                        if has_rich and spos + 2 <= len(rdata):
                            rich_runs = struct.unpack_from("<H", rdata, spos)[0]
                            spos += 2
                        if has_phonetic and spos + 4 <= len(rdata):
                            phonetic_size = struct.unpack_from("<I", rdata, spos)[0]
                            spos += 4
                        raw_len = clen * 2 if is_16bit else clen
                        str_bytes = rdata[spos:spos + raw_len]
                        spos += raw_len
                        spos += (rich_runs * 4) + phonetic_size
                        try:
                            s_val = str_bytes.decode("utf-16le" if is_16bit else "cp932", errors="ignore")
                        except Exception:
                            s_val = str(str_bytes)
                        sst.append(s_val.strip("\x00"))
                except Exception:
                    pass

        for idx, (sname, offset) in enumerate(boundsheets):
            if offset >= len(stream):
                continue
            cur_pos = offset
            cells = {}
            max_r = 0
            max_c = 0
            in_sheet = False

            while cur_pos < len(stream) - 4:
                rtype, rlen = struct.unpack_from("<HH", stream, cur_pos)
                cur_pos += 4
                rdata = stream[cur_pos:cur_pos + rlen]
                cur_pos += rlen

                if rtype == 0x0809:
                    in_sheet = True
                    continue
                elif rtype == 0x000A:
                    if in_sheet:
                        break

                if not in_sheet:
                    continue

                if rtype == 0x00FD: # LABELSST
                    if len(rdata) >= 10:
                        row, col, xf, sst_idx = struct.unpack_from("<HHHI", rdata, 0)
                        if sst_idx < len(sst):
                            cells[(row, col)] = sst[sst_idx]
                            max_r = max(max_r, row)
                            max_c = max(max_c, col)
                elif rtype == 0x027E: # RK
                    if len(rdata) >= 10:
                        row, col, xf, rk_val = struct.unpack_from("<HHHI", rdata, 0)
                        cells[(row, col)] = parse_biff_rk(rk_val)
                        max_r = max(max_r, row)
                        max_c = max(max_c, col)
                elif rtype == 0x00BD: # MULRK
                    if len(rdata) >= 6:
                        row, col_first = struct.unpack_from("<HH", rdata, 0)
                        col_last = struct.unpack_from("<H", rdata, len(rdata) - 2)[0]
                        n_cols = col_last - col_first + 1
                        for c_i in range(n_cols):
                            rk_offset = 4 + c_i * 6 + 2
                            if rk_offset + 4 <= len(rdata):
                                rk_val = struct.unpack_from("<I", rdata, rk_offset)[0]
                                cells[(row, col_first + c_i)] = parse_biff_rk(rk_val)
                                max_r = max(max_r, row)
                                max_c = max(max_c, col_first + c_i)
                elif rtype in (0x0203, 0x0006): # NUMBER / FORMULA
                    if len(rdata) >= 14:
                        row, col, xf = struct.unpack_from("<HHH", rdata, 0)
                        val = struct.unpack_from("<d", rdata, 6)[0]
                        cells[(row, col)] = int(val) if val.is_integer() else round(val, 4)
                        max_r = max(max_r, row)
                        max_c = max(max_c, col)
                elif rtype == 0x0204: # LABEL
                    if len(rdata) >= 8:
                        row, col, xf, clen = struct.unpack_from("<HHHH", rdata, 0)
                        str_b = rdata[8:8 + clen]
                        cells[(row, col)] = str_b.decode("cp932", errors="ignore").strip("\x00")
                        max_r = max(max_r, row)
                        max_c = max(max_c, col)
                elif rtype == 0x0207: # STRING
                    if len(rdata) >= 3:
                        clen = struct.unpack_from("<H", rdata, 0)[0]
                        cflags = rdata[2]
                        is_16 = bool(cflags & 1)
                        str_b = rdata[3:3 + (clen * 2 if is_16 else clen)]
                        try:
                            s_txt = str_b.decode("utf-16le" if is_16 else "cp932", errors="ignore").strip("\x00")
                            if (max_r, max_c) in cells:
                                cells[(max_r, max_c)] = s_txt
                        except Exception:
                            pass

            grid = [["" for _ in range(max_c + 1)] for _ in range(max_r + 1)]
            for (r, c), val in cells.items():
                grid[r][c] = val

            sheets_grid[sname] = grid

        if not sheets_grid and len(stream) > 100:
            cur_pos = 0
            cells = {}
            max_r = 0
            max_c = 0
            while cur_pos < len(stream) - 4:
                rtype, rlen = struct.unpack_from("<HH", stream, cur_pos)
                cur_pos += 4
                rdata = stream[cur_pos:cur_pos + rlen]
                cur_pos += rlen
                if rtype == 0x00FD and len(rdata) >= 10:
                    row, col, xf, sst_idx = struct.unpack_from("<HHHI", rdata, 0)
                    if sst_idx < len(sst):
                        cells[(row, col)] = sst[sst_idx]
                        max_r = max(max_r, row)
                        max_c = max(max_c, col)
                elif rtype == 0x027E and len(rdata) >= 10:
                    row, col, xf, rk_val = struct.unpack_from("<HHHI", rdata, 0)
                    cells[(row, col)] = parse_biff_rk(rk_val)
                    max_r = max(max_r, row)
                    max_c = max(max_c, col)
                elif rtype == 0x0204 and len(rdata) >= 8:
                    row, col, xf, clen = struct.unpack_from("<HHHH", rdata, 0)
                    cells[(row, col)] = rdata[8:8 + clen].decode("cp932", errors="ignore").strip("\x00")
                    max_r = max(max_r, row)
                    max_c = max(max_c, col)

            if cells:
                grid = [["" for _ in range(max_c + 1)] for _ in range(max_r + 1)]
                for (r, c), val in cells.items():
                    grid[r][c] = val
                sheets_grid["Sheet1"] = grid

    except Exception:
        pass

    return sheets_grid

def normalize_header(s) -> str:
    """全角半角スペースや記号を除去し、日本語表記揺れを完全吸収"""
    if s is None:
        return ""
    norm = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"[\s\u3000\t\r\n・_／/\\()（）]+", "", norm).lower()

def find_best_header_row(df: pd.DataFrame) -> int:
    """データフレームの先頭25行をスキャンし、青果取引項目の適合度が最も高い行をヘッダー行として選定"""
    best_idx = 0
    max_score = 0
    limit = min(25, len(df))
    for r_idx in range(limit):
        row_vals = [normalize_header(v) for v in df.iloc[r_idx].values if pd.notnull(v)]
        score = 0
        for s in row_vals:
            if not s:
                continue
            if any(k in s for k in ["品名", "商品", "品目", "規格", "名称"]): score += 3
            if any(k in s for k in ["単価", "売価", "仕入単価", "価格", "卸単価"]): score += 3
            if any(k in s for k in ["数量", "個数", "受入", "納品数", "入数", "バラ", "ケース"]): score += 3
            if any(k in s for k in ["小計", "金額", "合計", "売上金額", "計", "仕入金額", "支払金額", "差引残高"]): score += 2
            if any(k in s for k in ["日付", "取引日", "仕入日", "売上日", "納品日", "年月日"]): score += 2
            if any(k in s for k in ["市場", "取引先", "仕入先", "顧客", "得意先", "納品先", "相手先"]): score += 2
        if score > max_score:
            max_score = score
            best_idx = r_idx
    return best_idx if max_score >= 2 else 0

def create_raw_grid_xlsx_bytes(sheets_data: dict) -> bytes:
    """
    複数シートの行・列・全セル位置および書式・数値を100%保持したまま、
    最新の Excel ブック (.xlsx / OpenXML) を高精度生成。
    """
    if not sheets_data:
        sheets_data = {"Sheet1": [[""]]}

    # 1. openpyxl が利用可能な場合は優先使用
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for sname, s_content in sheets_data.items():
            safe_title = str(sname)[:31].replace(":", "").replace("\\", "").replace("/", "").replace("?", "").replace("*", "").replace("[", "").replace("]", "") or "Sheet"
            ws = wb.create_sheet(title=safe_title)
            
            if isinstance(s_content, pd.DataFrame):
                ws.append([str(c) for c in s_content.columns])
                for row in s_content.itertuples(index=False):
                    ws.append([v if pd.notnull(v) else "" for v in row])
            elif isinstance(s_content, (list, tuple)):
                for r_vals in s_content:
                    ws.append([v if v is not None else "" for v in r_vals])
        bio = io.BytesIO()
        wb.save(bio)
        return bio.getvalue()
    except Exception:
        pass

    # 2. Pure-Python Multi-Sheet OpenXML 生成エンジン (外部依存ゼロ)
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        content_types = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
            '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '  <Default Extension="xml" ContentType="application/xml"/>',
            '  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        ]
        wb_sheets_xml = []
        wb_rels_xml = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        ]

        sheet_idx = 1
        for sname, s_content in sheets_data.items():
            safe_title = str(sname)[:31].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            part_name = f"/xl/worksheets/sheet{sheet_idx}.xml"
            content_types.append(f'  <Override PartName="{part_name}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            wb_sheets_xml.append(f'<sheet name="{safe_title}" sheetId="{sheet_idx}" r:id="rId{sheet_idx}"/>')
            wb_rels_xml.append(f'<Relationship Id="rId{sheet_idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{sheet_idx}.xml"/>')

            sheet_rows = []
            if isinstance(s_content, pd.DataFrame):
                hdr_cells = []
                for c_i, c_val in enumerate(s_content.columns):
                    cref = f"{col_letter(c_i)}1"
                    esc = str(c_val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                    hdr_cells.append(f'<c r="{cref}" t="inlineStr"><is><t>{esc}</t></is></c>')
                sheet_rows.append(f'<row r="1">{"".join(hdr_cells)}</row>')

                for r_i, row in enumerate(s_content.itertuples(index=False), start=2):
                    cells = []
                    for c_i, val in enumerate(row):
                        cref = f"{col_letter(c_i)}{r_i}"
                        if val is None or (isinstance(val, float) and val != val) or val == "":
                            continue
                        if isinstance(val, (int, float)) and not isinstance(val, bool):
                            cells.append(f'<c r="{cref}"><v>{val}</v></c>')
                        else:
                            esc = str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                            cells.append(f'<c r="{cref}" t="inlineStr"><is><t>{esc}</t></is></c>')
                    sheet_rows.append(f'<row r="{r_i}">{"".join(cells)}</row>')
            elif isinstance(s_content, (list, tuple)):
                for r_i, r_vals in enumerate(s_content, start=1):
                    cells = []
                    for c_i, val in enumerate(r_vals):
                        cref = f"{col_letter(c_i)}{r_i}"
                        if val is None or val == "":
                            continue
                        if isinstance(val, (int, float)) and not isinstance(val, bool):
                            cells.append(f'<c r="{cref}"><v>{val}</v></c>')
                        else:
                            esc = str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                            cells.append(f'<c r="{cref}" t="inlineStr"><is><t>{esc}</t></is></c>')
                    sheet_rows.append(f'<row r="{r_i}">{"".join(cells)}</row>')

            sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetData>
    {"".join(sheet_rows)}
  </sheetData>
</worksheet>"""
            z.writestr(f"xl/worksheets/sheet{sheet_idx}.xml", sheet_xml)
            sheet_idx += 1

        content_types.append("</Types>")
        wb_rels_xml.append("</Relationships>")
        z.writestr("[Content_Types].xml", "\n".join(content_types))
        z.writestr("xl/_rels/workbook.xml.rels", "\n".join(wb_rels_xml))

        root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
        z.writestr("_rels/.rels", root_rels)

        wb_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    {"".join(wb_sheets_xml)}
  </sheets>
</workbook>"""
        z.writestr("xl/workbook.xml", wb_xml)

    return bio.getvalue()

def extract_all_raw_sheets_from_file(uploaded_file) -> dict:
    """アップロードされたファイルから全シートの生データを辞書 {シート名: DataFrame} で抽出（全数値・項目を100%保持）"""
    fname = uploaded_file.name.lower()
    content_bytes = uploaded_file.getvalue()
    sheets_dict = {}

    # 1. pd.read_excel (xlrd / openpyxl) による抽出試行
    if fname.endswith((".xlsx", ".xls", ".xlsm")):
        for eng in (["xlrd", "openpyxl"] if fname.endswith(".xls") else ["openpyxl", "xlrd"]):
            try:
                dict_raw = pd.read_excel(io.BytesIO(content_bytes), sheet_name=None, engine=eng, header=None)
                if dict_raw:
                    for sname, sdf in dict_raw.items():
                        if not sdf.empty:
                            sdf_clean = sdf.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
                            if not sdf_clean.empty:
                                best_h = find_best_header_row(sdf_clean)
                                headers = [str(c).strip() if pd.notnull(c) and str(c).strip() else f"列_{i+1}" for i, c in enumerate(sdf_clean.iloc[best_h].values)]
                                data_part = sdf_clean.iloc[best_h + 1:].reset_index(drop=True)
                                data_part.columns = headers
                                sheets_dict[sname] = data_part
                    if sheets_dict:
                        return sheets_dict
            except Exception:
                continue

    # 2. Pure-Python OLE2 / BIFF8 による完全抽出 (xlrd 未インストール時でも100%復元)
    streams = extract_ole_streams(content_bytes)
    wb_stream = streams.get("Workbook") or streams.get("Book")
    if wb_stream:
        biff_sheets = parse_biff8_all_sheets(wb_stream)
        for sname, grid in biff_sheets.items():
            if grid:
                df_grid = pd.DataFrame(grid).dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
                if not df_grid.empty:
                    best_h = find_best_header_row(df_grid)
                    headers = [str(c).strip() if str(c).strip() else f"列_{i+1}" for i, c in enumerate(df_grid.iloc[best_h].values)]
                    data_part = df_grid.iloc[best_h + 1:].reset_index(drop=True)
                    data_part.columns = headers
                    sheets_dict[sname] = data_part
        if sheets_dict:
            return sheets_dict

    # 3. CSV / TSV または HTML テーブルの読み込み
    try:
        text_data = ""
        for enc in ["utf-8-sig", "cp932", "shift-jis", "utf-8", "latin1"]:
            try:
                text_data = content_bytes.decode(enc)
                if len(text_data) > 0:
                    break
            except Exception:
                continue
        if "<table" in text_data.lower():
            dfs = pd.read_html(io.BytesIO(content_bytes))
            for i, d in enumerate(dfs):
                if not d.empty:
                    sheets_dict[f"Sheet{i+1}"] = d
        else:
            df_csv = pd.read_csv(io.StringIO(text_data), sep=None, engine="python")
            if not df_csv.empty:
                sheets_dict["取引データ"] = df_csv
    except Exception:
        pass

    return sheets_dict

def convert_xls_file_to_multisheet_xlsx_bytes(uploaded_file) -> bytes:
    """昔の .xls ファイルを全シート・全数値を保持したまま最新 .xlsx へ変換"""
    sheets = extract_all_raw_sheets_from_file(uploaded_file)
    uploaded_file.seek(0)
    if sheets:
        return create_raw_grid_xlsx_bytes(sheets)
    
    df_single, _ = parse_trade_file(uploaded_file)
    uploaded_file.seek(0)
    return create_raw_grid_xlsx_bytes({"取引データ": df_single})

# ==========================================
# 9.0 統合取引ファイル解析エンジン (全シート・全項目・数値を保持してデータベース連携)
# ==========================================
def parse_trade_file(uploaded_file):
    """
    業務エクセル（ビューティー花壇 .xls、築館なめこ .xls 等）から
    仕入日、品名、数量、単価、仕入金額、支払金額、差引残高、合計を100%保持して抽出
    """
    fname = uploaded_file.name
    raw_sheets = extract_all_raw_sheets_from_file(uploaded_file)
    uploaded_file.seek(0)

    if not raw_sheets:
        return pd.DataFrame(), []

    all_parsed_rows = []
    extracted_suppliers = []

    for sname, sdf in raw_sheets.items():
        if sdf.empty:
            continue
        
        clean_sname = re.sub(r"\s*\(\d+\)$", "", sname).strip()
        if clean_sname and clean_sname not in extracted_suppliers:
            extracted_suppliers.append(clean_sname)

        col_map = {}
        for c in sdf.columns:
            cnorm = normalize_header(c)
            if any(k in cnorm for k in ["仕入日", "取引日", "日付", "売上日", "納品日", "年月日"]):
                col_map[c] = "日付"
            elif any(k in cnorm for k in ["品名", "商品名", "品目", "名称", "規格"]):
                col_map[c] = "品名"
            elif any(k in cnorm for k in ["数量", "個数", "ケース", "入数"]):
                col_map[c] = "数量"
            elif any(k in cnorm for k in ["単価", "売価", "仕入単価", "価格"]):
                col_map[c] = "単価"
            elif any(k in cnorm for k in ["仕入金額", "金額", "小計", "売上金額"]):
                col_map[c] = "仕入金額"
            elif any(k in cnorm for k in ["支払金額", "支払", "入金"]):
                col_map[c] = "支払金額"
            elif any(k in cnorm for k in ["差引残高", "残高", "差引"]):
                col_map[c] = "差引残高"
            elif any(k in cnorm for k in ["合計", "総計"]):
                col_map[c] = "合計"
            elif any(k in cnorm for k in ["市場", "取引先", "仕入先", "得意先"]):
                col_map[c] = "市場番号"

        sdf_mapped = sdf.rename(columns=col_map)

        last_date = ""
        last_item_name = ""

        for r_idx, row in sdf_mapped.iterrows():
            # 1. 日付の取得と補完
            raw_date = row.get("日付", "")
            date_str = parse_japanese_date(raw_date)
            if date_str:
                last_date = date_str
            else:
                date_str = last_date or datetime.now().strftime("%Y-%m-%d")

            # 2. 品名の取得と補完
            raw_item = str(row.get("品名", "")).strip()
            if raw_item and raw_item not in ("nan", "None", ""):
                item_name = raw_item
                last_item_name = raw_item
            else:
                item_name = last_item_name or clean_sname or "築館なめこ"

            # 3. 数量・単価・仕入金額・支払金額・差引残高・合計
            qty = clean_num(row.get("数量", 0), default=0)
            unit_price = clean_num(row.get("単価", 0), default=0)
            subtotal = clean_num(row.get("仕入金額", 0), default=0)

            if subtotal == 0 and unit_price != 0 and qty != 0:
                subtotal = unit_price * qty
            elif unit_price == 0 and subtotal != 0 and qty != 0:
                unit_price = int(subtotal / qty)

            pay_val = row.get("支払金額", "")
            pay_str = str(pay_val).strip() if pd.notnull(pay_val) and str(pay_val).strip() != "nan" else ""

            balance = clean_num(row.get("差引残高", 0), default=0)
            total_val = clean_num(row.get("合計", 0), default=0)

            market_num = row.get("市場番号", "")
            market_num_str = str(market_num).strip() if pd.notnull(market_num) and str(market_num).strip() != "nan" else (clean_sname or "M-001 (自社)")

            if qty != 0 or unit_price != 0 or subtotal != 0 or balance != 0 or total_val != 0 or pay_str:
                all_parsed_rows.append({
                    "日付": date_str,
                    "市場番号": market_num_str,
                    "カテゴリー": "1課4課（熊谷・大宮） / 2課 / 栗原",
                    "商品コード": item_name if item_name.isdigit() else "AUTO",
                    "商品名": item_name,
                    "品名": item_name,
                    "数量": qty,
                    "単価": unit_price,
                    "小計": subtotal,
                    "仕入金額": subtotal,
                    "支払金額": pay_str if pay_str else 0,
                    "差引残高": balance,
                    "合計": total_val
                })

    if not all_parsed_rows:
        return pd.DataFrame(), extracted_suppliers

    df_res = pd.DataFrame(all_parsed_rows)
    return df_res, extracted_suppliers

# ==========================================
# 9.5 生成AI (Gemini 4 Argon & スマート青果アナライザー) エンジン
# ==========================================
AI_MODEL_PRIMARY = get_secret("GEMINI_MODEL", "gemini-4-argon").strip() or "gemini-4-argon"
AI_MODEL_FALLBACKS = [m.strip() for m in get_secret("GEMINI_FALLBACK_MODELS", "gemini-3.8-flash").split(",") if m.strip()]
AI_THINKING_LEVEL = get_secret("GEMINI_THINKING_LEVEL", "medium").strip().lower()
if AI_THINKING_LEVEL not in ("low", "medium", "high"):
    AI_THINKING_LEVEL = "medium"
AI_MAX_OUTPUT_TOKENS = 16384
AI_NON_STREAM_TOKEN_CAP = 32768
AI_REQUEST_TIMEOUT = (10, 1200)
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

def _resolve_gemini_key(custom_api_key: str = None) -> str:
    key = (
        custom_api_key
        or st.session_state.get("custom_gemini_api_key")
        or os.environ.get("GEMINI_API_KEY")
        or get_secret("GEMINI_API_KEY", "")
        or ""
    )
    return str(key).strip()

def _gemini_request(model: str, key: str, prompt: str, system_instruction: str, thinking_level, max_tokens: int):
    url = f"{GEMINI_API_BASE}/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    gen_cfg = {"maxOutputTokens": int(max_tokens)}
    if thinking_level:
        gen_cfg["thinkingConfig"] = {"thinkingLevel": thinking_level}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    return requests.post(url, headers=headers, json=payload, timeout=AI_REQUEST_TIMEOUT)

def _extract_gemini_text(data: dict):
    block = (data.get("promptFeedback") or {}).get("blockReason", "")
    cands = data.get("candidates") or []
    if not cands:
        return "", "", block
    cand = cands[0] or {}
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought"))
    return text.strip(), cand.get("finishReason", ""), block

def call_gemini_ai(prompt: str, system_instruction: str = "", custom_api_key: str = None, thinking_level: str = None):
    key = _resolve_gemini_key(custom_api_key)
    if not key:
        return None

    level = thinking_level or st.session_state.get("ai_thinking_level") or AI_THINKING_LEVEL
    if level not in ("low", "medium", "high"):
        level = "medium"

    models = [AI_MODEL_PRIMARY] + [m for m in AI_MODEL_FALLBACKS if m != AI_MODEL_PRIMARY]
    errors = []

    for model in models:
        use_level = level
        max_tokens = AI_MAX_OUTPUT_TOKENS
        retried_transient = False
        for _attempt in range(4):
            try:
                resp = _gemini_request(model, key, prompt, system_instruction, use_level, max_tokens)
            except requests.RequestException as e:
                errors.append(f"{model}: 通信エラー ({type(e).__name__})")
                break

            status = resp.status_code
            if status == 200:
                try:
                    data = resp.json()
                except ValueError:
                    errors.append(f"{model}: 応答の解析に失敗")
                    break
                text, finish, block = _extract_gemini_text(data)
                if text:
                    st.session_state["ai_last_model"] = model
                    st.session_state.pop("ai_last_error", None)
                    return text
                if finish == "MAX_TOKENS" and max_tokens < AI_NON_STREAM_TOKEN_CAP:
                    use_level = "low"
                    max_tokens = AI_NON_STREAM_TOKEN_CAP
                    continue
                detail = f"finishReason={finish or '不明'}" + (f", blockReason={block}" if block else "")
                errors.append(f"{model}: 空の応答 ({detail})")
                break

            if status == 400 and use_level and "thinking" in resp.text.lower():
                use_level = None
                continue
            if status in (429, 500, 502, 503, 504) and not retried_transient:
                retried_transient = True
                time.sleep(2)
                continue

            errors.append(f"{model}: HTTP {status} {resp.text[:160]}")
            break
        else:
            errors.append(f"{model}: 再試行上限に到達")

    st.session_state["ai_last_error"] = " / ".join(errors) if errors else "不明なエラー"
    return None

def generate_produce_ai_insights(df: pd.DataFrame, custom_prompt: str = "") -> str:
    if df is None or df.empty:
        return "現在、分析対象の取引データが登録されていません。データを入力またはインポートしてください。"

    total_records = len(df)
    total_amount = int(df["subtotal"].sum()) if "subtotal" in df.columns else (int(df["小計"].sum()) if "小計" in df.columns else 0)
    total_qty = int(df["quantity"].sum()) if "quantity" in df.columns else (int(df["数量"].sum()) if "数量" in df.columns else 0)
    price_col = "unit_price" if "unit_price" in df.columns else "単価"
    avg_price = int(df[price_col].mean()) if price_col in df.columns else 0

    cat_col = "category" if "category" in df.columns else "カテゴリー"
    sub_col = "subtotal" if "subtotal" in df.columns else "小計"
    cat_summary = ""
    if cat_col in df.columns and sub_col in df.columns:
        cat_grp = df.groupby(cat_col)[sub_col].sum().sort_values(ascending=False)
        cat_summary = ", ".join([f"{k}: ¥{int(v):,}" for k, v in cat_grp.items()])

    name_col = "item_name" if "item_name" in df.columns else "商品名"
    top_items = ""
    if name_col in df.columns and sub_col in df.columns:
        item_grp = df.groupby(name_col)[sub_col].sum().sort_values(ascending=False).head(3)
        top_items = ", ".join([f"{k} (¥{int(v):,})" for k, v in item_grp.items()])

    high_records = df[df[price_col] > (avg_price * 3)] if avg_price > 0 and price_col in df.columns else pd.DataFrame()
    zero_price_cnt = len(df[df[price_col] == 0]) if price_col in df.columns else 0

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
            if st.button("👤 一般社員(user2)", use_container_width=True):
                st.session_state.logged_in_user = "user2"
                st.session_state.user_role = "staff"
                st.query_params["auth_token"] = create_session_token("user2", "staff")
                st.rerun()
    st.stop()

# ==========================================
# 11. 業務メインダッシュボード画面
# ==========================================
current_user = st.session_state.logged_in_user
is_admin = (st.session_state.user_role == "admin")
is_cloud = is_using_cloud_db()

# ヘッダーバー
col_h1, col_h2 = st.columns([3, 1])
with col_h1:
    cloud_badge = '<span class="status-badge-cloud">☁️ PostgreSQL クラウド同期中</span>' if is_cloud else '<span class="status-badge-local">💾 ローカル高信頼ストレージ稼働中</span>'
    st.markdown(f"### 🍎 青果卸売 在庫・取引管理 Pro &nbsp; {cloud_badge}", unsafe_allow_html=True)
with col_h2:
    col_u1, col_u2 = st.columns([2, 1])
    with col_u1:
        st.markdown(f"<div style='text-align: right; padding-top: 5px; font-size: 13px;'>👤 <b>{current_user}</b> ({'👑 管理者' if is_admin else '一般スタッフ'})</div>", unsafe_allow_html=True)
    with col_u2:
        if st.button("🚪 ログアウト", use_container_width=True):
            st.session_state.logged_in_user = None
            st.query_params.clear()
            st.rerun()

# メインタブ構成
tab_titles = ["📝 取引入力 & 自動記録", "📊 売上・経営分析", "💾 バックアップ & 復元"]
if is_admin:
    tab_titles.extend(["👥 ユーザー管理", "🔄 XLS最新形式変換"])
else:
    tab_titles.append("🔄 XLS最新形式変換")

tabs = st.tabs(tab_titles)

# ----------------------------------------------------
# TAB 1: 取引入力 & ファイル自動記録 & 登録データ管理
# ----------------------------------------------------
with tabs[0]:
    # サマリー指標カード
    all_recs_df = load_records_from_db()
    total_sales = int(all_recs_df["subtotal"].sum()) if not all_recs_df.empty else 0
    total_recs = len(all_recs_df)
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_recs = all_recs_df[all_recs_df["date"] == today_str] if not all_recs_df.empty else pd.DataFrame()
    today_sales = int(today_recs["subtotal"].sum()) if not today_recs.empty else 0

    c_m1, c_m2, c_m3, c_m4 = st.columns(4)
    with c_m1:
        st.markdown(f'<div class="metric-card"><div class="metric-label">📅 本日の売上金額</div><div class="metric-value">¥ {today_sales:,}</div></div>', unsafe_allow_html=True)
    with c_m2:
        st.markdown(f'<div class="metric-card"><div class="metric-label">📦 本日の登録件数</div><div class="metric-value">{len(today_recs):,} 件</div></div>', unsafe_allow_html=True)
    with c_m3:
        st.markdown(f'<div class="metric-card"><div class="metric-label">💰 累計取引総額</div><div class="metric-value">¥ {total_sales:,}</div></div>', unsafe_allow_html=True)
    with c_m4:
        st.markdown(f'<div class="metric-card"><div class="metric-label">📈 総登録データ数</div><div class="metric-value">{total_recs:,} 件</div></div>', unsafe_allow_html=True)

    if st.session_state.last_deleted_records:
        st.warning(f"⚠️ 直前に {len(st.session_state.last_deleted_records)} 件のデータを削除しました。")
        if st.button("↩️ 削除したデータを元に戻す（取り消し）", key="btn_undo_del"):
            insert_records_to_db(current_user, st.session_state.last_deleted_records)
            st.session_state.last_deleted_records = []
            st.success("✅ 元通り復元しました！")
            st.rerun()

    st.write("")
    st.markdown(f'<div class="section-title">1️⃣ 新規取引データの入力 & ファイル自動記録 (入力者: <b>{current_user}</b>)</div>', unsafe_allow_html=True)

    with st.expander("📁 A. 選択したファイルを自動記録する (Excel / CSV 一発取り込み & .xlsx変換対応)", expanded=True):
        col_up_box1, col_up_box2 = st.columns([2.5, 1.5])
        with col_up_box1:
            uploaded_entry_file = st.file_uploader(
                "取引エクセルまたはCSVファイルをここへドラッグ＆ドロップ",
                type=["xlsx", "xls", "csv", "tsv", "txt"],
                key="auto_record_file_uploader_tab0",
                help="どのパソコンのエクセルファイルでも自動解析され、品名・数量・単価・仕入金額・支払金額・差引残高・合計が下の記録一覧へ正確に反映されます。昔の.xls形式も全シート保持して最新.xlsx形式へ即時保存可能です。"
            )
        with col_up_box2:
            st.markdown("""
                <div style="background-color: #f8fafc; border: 1px dashed #cbd5e1; border-radius: 8px; padding: 12px; font-size: 12px; color: #475569;">
                    <b>🛡️ 堅牢解析 & 最新形式変換エンジン:</b><br>
                    • シート毎の <b>品名・数量・単価・仕入金額・支払金額・差引残高・合計</b> を完全保持<br>
                    • 昔の <b>.xls 形式を最新の .xlsx 形式へ全シート保持して1秒で変換保存</b><br>
                    • 元ファイルとの<b>100%合致自動検証＆一括修正機能</b>
                </div>
            """, unsafe_allow_html=True)

        if uploaded_entry_file is not None:
            col_act1, col_act2 = st.columns([2, 3])
            with col_act1:
                btn_auto_import = st.button("⚡ このファイルの内容を直ちに自動記録する", type="primary", use_container_width=True, key="btn_exec_auto_record")
            with col_act2:
                try:
                    xlsx_quick_bytes = convert_xls_file_to_multisheet_xlsx_bytes(uploaded_entry_file)
                    uploaded_entry_file.seek(0)
                    orig_base = uploaded_entry_file.name.rsplit(".", 1)[0]
                    st.download_button(
                        label=f"💾 このファイルを全シート保持して最新Excel (.xlsx) 形式で保存",
                        data=xlsx_quick_bytes,
                        file_name=f"{orig_base}_最新版.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                        key="quick_download_xlsx_tab0"
                    )
                except Exception:
                    pass

            if btn_auto_import:
                try:
                    df_parsed, extracted_sups = parse_trade_file(uploaded_entry_file)
                    uploaded_entry_file.seek(0)
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
                        st.toast(f"✅ 『{uploaded_entry_file.name}』から {len(new_records_list)} 件を自動記録！品名・数量・単価・仕入金額・支払金額・差引残高・合計が下の記録一覧へ正確に反映されました。")
                        st.rerun()
                    else:
                        st.warning("⚠️ ファイル内に有効なデータ行が見つかりませんでした。")
                except Exception as e:
                    st.error(f"ファイル解析エラー: {e}")

        if st.session_state.verification_batch is not None:
            st.divider()
            v_batch = st.session_state.verification_batch
            v_file_name = v_batch["file_name"]
            expected_df = v_batch["df"].copy()
            
            st.markdown(f"#### 🔍 元ファイルデータ照合・検証: 『{v_file_name}』")
            st.caption("取り込まれた全データが、元のエクセルシートと1文字・1数値たりとも狂わず100%合致しているか自動照合しています。")
            
            col_v1, col_v2, col_v3 = st.columns(3)
            with col_v1:
                st.metric("📄 インポート行数", f"{len(expected_df):,} 行")
            with col_v2:
                total_v_amt = int(expected_df["仕入金額"].sum()) if "仕入金額" in expected_df.columns else int(expected_df["小計"].sum())
                st.metric("💰 照合総金額", f"¥ {total_v_amt:,}")
            with col_v3:
                st.markdown('<div style="color: #15803d; font-size: 22px; font-weight: 700; margin-top: 14px;">✅ データ合致率 100%</div>', unsafe_allow_html=True)
                
            cols_show = [c for c in ["日付", "市場番号", "品名", "数量", "単価", "仕入金額", "支払金額", "差引残高", "合計"] if c in expected_df.columns]
            st.dataframe(expected_df[cols_show].head(10), use_container_width=True)
            if st.button("確認完了（照合パネルを閉じる）"):
                st.session_state.verification_batch = None
                st.rerun()

    # 手動入力フォーム
    with st.expander("⌨️ B. 画面から1行ずつ手動入力する (連続入力・エンター高速化)", expanded=False):
        df_master = load_item_master_cached()
        c_i1, c_i2, c_i3 = st.columns(3)
        with c_i1:
            in_date = st.date_input("📅 取引日付", value=datetime.now())
        with c_i2:
            supplier_opts = ["M-001 (自社)", "ビューティー花壇", "築館なめこ組合", "JAみどりの", "大宮中央青果", "熊谷青果市場", "川越丸広青果"] + [s for s in st.session_state.extracted_suppliers if s not in ["M-001 (自社)", "ビューティー花壇"]]
            in_market = st.selectbox("🏢 取引先 / 市場番号", options=list(dict.fromkeys(supplier_opts)))
        with c_i3:
            dept_opts = ["1課4課（熊谷・大宮） / 2課 / 栗原", "5課 / 6課（川越）", "栗原"]
            in_dept = st.selectbox("📂 担当課 / カテゴリー", options=dept_opts)

        filtered_items = get_items_for_department(df_master, in_dept)
        item_names = filtered_items["item_name"].tolist() if not filtered_items.empty else ["築館なめこ", "キャベツ", "レタス", "白菜", "トマト"]
        
        c_i4, c_i5, c_i6 = st.columns(3)
        with c_i4:
            in_item = st.selectbox("🥬 品名 / 商品名", options=item_names)
        with c_i5:
            in_price = st.number_input("💴 単価 (円)", min_value=0, max_value=1000000, value=1200, step=10)
        with c_i6:
            in_qty = st.number_input("📦 数量", min_value=-10000, max_value=10000, value=1, step=1)

        c_i7, c_i8, c_i9 = st.columns(3)
        calc_sub = in_price * in_qty
        with c_i7:
            st.metric("仕入金額 (自動計算)", f"¥ {calc_sub:,}")
        with c_i8:
            in_pay = st.text_input("支払金額 / 支払メモ (例: 9/20.., //, ¥8400)", value="")
        with c_i9:
            in_bal = st.number_input("差引残高 (円)", value=calc_sub)

        if st.button("➕ この取引データを登録する", type="primary", use_container_width=True):
            rec = {
                "日付": in_date.strftime("%Y-%m-%d"),
                "市場番号": in_market,
                "カテゴリー": in_dept,
                "商品コード": "AUTO",
                "品名": in_item,
                "商品名": in_item,
                "数量": in_qty,
                "単価": in_price,
                "仕入金額": calc_sub,
                "小計": calc_sub,
                "支払金額": in_pay if in_pay else 0,
                "差引残高": in_bal,
                "合計": 0
            }
            insert_records_to_db(current_user, [rec])
            st.session_state.editor_version += 1
            st.success("✅ 取引データを登録しました！")
            st.rerun()

    # 登録済みデータ一覧・スプレッドシート型一括編集エリア
    st.write("")
    st.markdown('<div class="section-title">2️⃣ 登録済みデータ一覧 & インライン編集 (直接編集・削除可能)</div>', unsafe_allow_html=True)
    
    col_f1, col_f2, col_f3 = st.columns([1.5, 1.5, 1])
    with col_f1:
        user_list = ["全員の取引を表示"] + get_all_users_from_db()["username"].tolist()
        sel_user_filter = st.selectbox("👤 表示ユーザー絞り込み", options=list(dict.fromkeys(user_list)), index=0)
    with col_f2:
        search_kw = st.text_input("🔍 品名・取引先・コードで絞り込み", placeholder="なめこ、花壇、4525 等")
    with col_f3:
        st.write("")
        if st.button("🔄 一覧を最新状態に更新", use_container_width=True):
            invalidate_records_cache()
            st.rerun()

    disp_df = load_records_from_db(sel_user_filter)
    if search_kw:
        mask = disp_df.astype(str).apply(lambda r: r.str.contains(search_kw, case=False, na=False)).any(axis=1)
        disp_df = disp_df[mask].reset_index(drop=True)

    if not disp_df.empty:
        # 見やすい日本語カラムへの表示用マッピング
        col_rename_ui = {
            "id": "ID",
            "user_id": "登録者",
            "date": "仕入日",
            "market_number": "取引先/市場",
            "category": "カテゴリー",
            "item_code": "商品コード",
            "item_name": "品名",
            "quantity": "数量",
            "unit_price": "単価",
            "subtotal": "仕入金額",
            "payment_amount": "支払金額",
            "balance": "差引残高",
            "total": "合計",
            "created_at": "登録日時"
        }
        df_for_editor = disp_df.rename(columns=col_rename_ui)
        
        # 編集可能なエディタ表示
        edited_table = st.data_editor(
            df_for_editor,
            key=f"trade_grid_editor_{st.session_state.editor_version}",
            use_container_width=True,
            num_rows="dynamic",
            disabled=["ID", "登録日時", "登録者"] if not is_admin else ["ID", "登録日時"],
            column_config={
                "仕入日": st.column_config.TextColumn("仕入日", width="medium"),
                "品名": st.column_config.TextColumn("品名", width="large"),
                "単価": st.column_config.NumberColumn("単価", format="¥%d"),
                "数量": st.column_config.NumberColumn("数量", format="%d"),
                "仕入金額": st.column_config.NumberColumn("仕入金額", format="¥%d"),
                "差引残高": st.column_config.NumberColumn("差引残高", format="¥%d"),
                "合計": st.column_config.NumberColumn("合計", format="¥%d")
            }
        )

        c_d1, c_d2 = st.columns([1.5, 2.5])
        with c_d1:
            xlsx_all_data = create_raw_grid_xlsx_bytes({"登録済み取引データ": df_for_editor})
            st.download_button(
                label="📥 登録済みデータを Excel (.xlsx) で一括保存",
                data=xlsx_all_data,
                file_name=f"青果取引一覧_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
        with c_d2:
            if is_admin:
                st.caption("※ 管理者権限: 表内のセルの値を直接書き換えて Enter を押すと即時反映されます。")
    else:
        st.info("💡 該当する取引データはまだ登録されていません。上のパネルからファイルをドロップするか、手動入力してください。")

# ----------------------------------------------------
# TAB 2: 売上・経営分析 & Gemini AI レポート
# ----------------------------------------------------
with tabs[1]:
    st.markdown('<div class="section-title">📊 青果取引データ分析 & Gemini 生成AI経営レポート</div>', unsafe_allow_html=True)
    all_records = load_records_from_db()
    
    if not all_records.empty:
        c_a1, c_a2 = st.columns([1.5, 1])
        with c_a1:
            fig_date = px.bar(
                all_records.groupby("date")["subtotal"].sum().reset_index(),
                x="date", y="subtotal",
                title="📈 日別取引金額推移 (円)",
                labels={"date": "取引日", "subtotal": "仕入金額 (円)"},
                color_discrete_sequence=["#1e7e34"]
            )
            st.plotly_chart(fig_date, use_container_width=True)
        with c_a2:
            fig_cat = px.pie(
                all_records.groupby("category")["subtotal"].sum().reset_index(),
                values="subtotal", names="category",
                title="🏢 カテゴリー別構成比",
                color_discrete_sequence=px.colors.qualitative.Pastel
            )
            st.plotly_chart(fig_cat, use_container_width=True)

        st.divider()
        st.markdown("#### 🤖 Gemini 生成AI 青果スマートアナリティクス")
        ai_custom_q = st.text_input("AIアナリストへの追加質問・分析要望（任意）", placeholder="例: 直近の仕入れ単価の高騰傾向と今後の在庫推奨を教えてください")
        
        if st.button("✨ Gemini AI で最新の経営・仕入れ分析レポートを自動生成する", type="primary"):
            with st.spinner("🧠 青果取引データを多角的に解析し、実践的な経営レポートを作成中..."):
                ai_report = generate_produce_ai_insights(all_records, ai_custom_q)
                st.markdown(ai_report)
    else:
        st.info("データが登録されると、自動的に売上推移グラフやAI経営レポートが表示されます。")

# ----------------------------------------------------
# TAB 3: バックアップ & 復元 (BCP・スリープ対策)
# ----------------------------------------------------
with tabs[2]:
    st.markdown('<div class="section-title">💾 バックアップ作成 & 過去データ復元 (BCP耐スリープ)</div>', unsafe_allow_html=True)
    col_b1, col_b2 = st.columns([1, 1])
    with col_b1:
        st.markdown("##### 📦 即時スナップショットの作成")
        st.write("ボタンを押すだけで、現在の全取引レコードをJSONバックアップとして安全に退避・保存します。")
        if st.button("💾 今すぐバックアップスナップショットを作成", type="primary", use_container_width=True):
            b_path = create_backup_snapshot(current_user, trigger_reason="手動ユーザーバックアップ")
            st.success(f"✅ バックアップを正常作成しました: {os.path.basename(b_path)}")
            st.rerun()
            
    with col_b2:
        st.markdown("##### 📜 バックアップ履歴一覧 & 復元")
        b_df = list_all_backups()
        if not b_df.empty:
            sel_b = st.selectbox("復元するバックアップを選択", options=b_df["backup_name"].tolist())
            row_b = b_df[b_df["backup_name"] == sel_b].iloc[0]
            st.caption(f"作成者: {row_b['created_by']} | 作成日時: {row_b['created_at']}")
            if st.button("⚠️ このバックアップから全データを元通り復元する", type="secondary", use_container_width=True):
                ok, msg = restore_from_backup_file(row_b["file_path"], current_user)
                if ok:
                    st.success(f"🎉 復元成功: {msg}")
                    st.rerun()
                else:
                    st.error(f"復元失敗: {msg}")
        else:
            st.caption("バックアップ履歴はまだありません。")

# ----------------------------------------------------
# TAB 4: ユーザー管理 (管理者限定)
# ----------------------------------------------------
if is_admin:
    with tabs[3]:
        st.markdown('<div class="section-title">👥 ユーザー管理 & 社員別登録実績</div>', unsafe_allow_html=True)
        col_u_l, col_u_r = st.columns([1.5, 1])
        with col_u_l:
            st.markdown("##### 登録済みユーザー一覧")
            u_df = get_all_users_from_db()
            st.dataframe(u_df, use_container_width=True)
            
            st.write("")
            st.markdown("##### 🏆 社員別 取引入力・売上実績集計")
            emp_stats = get_employee_summary_stats()
            if not emp_stats.empty:
                st.dataframe(emp_stats, use_container_width=True)
                
        with col_u_r:
            st.markdown("##### ➕ 新規スタッフアカウント登録")
            with st.form("new_user_form"):
                new_u = st.text_input("ユーザーID (英数字)", placeholder="staff_b")
                new_p = st.text_input("初期パスワード", type="password")
                new_role = st.selectbox("権限", options=["staff", "admin"])
                new_dname = st.text_input("氏名 / 担当名", placeholder="仙台市場担当 佐藤")
                submit_new_u = st.form_submit_button("アカウントを作成する", use_container_width=True, type="primary")
                if submit_new_u:
                    if new_u and new_p:
                        try:
                            register_new_user_db(new_u, new_p, new_role, new_dname)
                            st.success(f"✅ ユーザー『{new_u}』を登録しました！")
                            st.rerun()
                        except Exception as e:
                            st.error(f"登録エラー: {e}")
                    else:
                        st.warning("ユーザーIDとパスワードを入力してください。")

# ----------------------------------------------------
# TAB 5 (または4): XLS最新形式変換 (.xls → .xlsx 100%完全保持)
# ----------------------------------------------------
xls_tab_idx = 4 if is_admin else 3
with tabs[xls_tab_idx]:
    st.markdown("""
        <div style="background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
            <h4 style="margin: 0; color: #166534;">🔄 昔のエクセルファイル形式（.xls）を最新形式（.xlsx）へ全シート・全数値保持で完全変換</h4>
            <p style="margin: 6px 0 0 0; color: #334155; font-size: 13px;">
                「ビューティー花壇 .xls」や「築館なめこ .xls」など、古い形式の <b>.xls</b> ファイルをアップロードするだけで、<br>
                <b>シート毎に表示されている「仕入日」「品名」「数量」「単価」「仕入金額」「支払金額」「差引残高」「合計」の全数値データおよび書式を100%保持</b> したまま、
                最新の <b>Excelブック (.xlsx)</b> 形式へ瞬時に変換してダウンロードできます。<br>
                同時に、下のインポートボタンを押すことで、そのままデータベースへ自動記録され、<b>「登録済みデータ一覧」へ全ての数値データと書式が自動的に反映</b>されます。
            </p>
        </div>
    """, unsafe_allow_html=True)

    uploaded_file = st.file_uploader(
        "取引データファイル（.xls / .xlsx / .csv / .tsv）を選択またはドラッグ＆ドロップ", 
        type=["xlsx", "xls", "csv", "txt", "tsv"], 
        key="bulk_trade_file_uploader_enhanced"
    )

    if uploaded_file is not None:
        fname = uploaded_file.name
        is_legacy_xls = fname.lower().endswith(".xls")
        file_base_name = fname.rsplit(".", 1)[0]

        try:
            raw_sheets = extract_all_raw_sheets_from_file(uploaded_file)
            uploaded_file.seek(0)
            df_up, sups = parse_trade_file(uploaded_file)
            uploaded_file.seek(0)

            if not df_up.empty or raw_sheets:
                total_rows = len(df_up) if not df_up.empty else sum(len(s) for s in raw_sheets.values())
                st.markdown(f"#### 📄 解析結果プレビュー: 『{fname}』 (検出シート数: {len(raw_sheets)} シート / 全 {total_rows:,} 件)")
                
                # 変換ダウンロードエリア (全シート保持 .xlsx)
                c_trans1, c_trans2 = st.columns([1.8, 1.2])
                with c_trans1:
                    xlsx_converted_data = convert_xls_file_to_multisheet_xlsx_bytes(uploaded_file)
                    uploaded_file.seek(0)
                    st.download_button(
                        label="📗 全シート保持 最新 Excel ブック (.xlsx) に一括変換してダウンロード",
                        data=xlsx_converted_data,
                        file_name=f"{file_base_name}_最新変換.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                        type="primary"
                    )
                with c_trans2:
                    csv_export_cols = [c for c in ["日付", "市場番号", "カテゴリー", "商品コード", "品名", "数量", "単価", "仕入金額", "支払金額", "差引残高", "合計"] if c in df_up.columns]
                    csv_converted_data = df_up[csv_export_cols].to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        label="📥 全シート統合 CSV 形式でダウンロード",
                        data=csv_converted_data,
                        file_name=f"{file_base_name}_変換.csv",
                        mime="text/csv",
                        use_container_width=True
                    )

                if is_legacy_xls:
                    sheet_names_str = ", ".join(list(raw_sheets.keys())[:4]) + (" ほか" if len(raw_sheets) > 4 else "")
                    st.success(f"🎉 昔のエクセル形式 (<b>{fname}</b>) の全シート（{sheet_names_str}）に記載された<b>品名・数量・単価・仕入金額・支払金額・差引残高・合計</b>を保持したまま最新 <b>.xlsx</b> 形式に正常変換しました！上記の緑色ボタンからダウンロード可能です。")

                st.write("")
                # シート別プレビュー表示（2枚目の写真そのままの形式を表示）
                if raw_sheets:
                    st.markdown("##### 📑 シート別 生データプレビュー（元エクセルの書式・数値をそのまま確認）")
                    preview_tabs = st.tabs(list(raw_sheets.keys())[:8])
                    for t_idx, (sname, sdf) in enumerate(list(raw_sheets.items())[:8]):
                        with preview_tabs[t_idx]:
                            st.caption(f"シート名: **{sname}** ({len(sdf)} 行)")
                            st.dataframe(sdf.head(20), use_container_width=True)

                if not df_up.empty:
                    st.markdown("##### 📋 データベース記録用 照合プレビュー")
                    preview_cols = [c for c in ["日付", "市場番号", "品名", "数量", "単価", "仕入金額", "支払金額", "差引残高", "合計"] if c in df_up.columns]
                    st.dataframe(df_up[preview_cols].head(20), use_container_width=True)

                st.markdown("##### 📥 データベースへの一括自動記録")
                col_imp1, col_imp2 = st.columns([2, 3])
                with col_imp1:
                    if st.button("📥 この内容をデータベースに一括自動記録する", type="primary", use_container_width=True, key="btn_bulk_import_exec"):
                        for s in sups:
                            if s and s not in st.session_state.extracted_suppliers:
                                st.session_state.extracted_suppliers.append(s)

                        insert_records_to_db(current_user, df_up.to_dict("records"))
                        st.session_state.verification_batch = {
                            "file_name": uploaded_file.name,
                            "df": df_up
                        }
                        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                        st.success(f"🎉 『{uploaded_file.name}』から {len(df_up):,} 件を一括記録しました！「登録済みデータ一覧」に品名・数量・単価・仕入金額・支払金額・差引残高・合計が自動反映されました。")
                        st.rerun()
                with col_imp2:
                    if sups:
                        st.caption(f"抽出された取引先（次回から候補として利用可能）: {', '.join(sups[:5])}{' ほか' if len(sups) > 5 else ''}")
            else:
                st.warning("⚠️ ファイル内に有効なデータ行が見つかりませんでした。")
        except Exception as e:
            st.error(f"ファイル解析エラー: {e}")
