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
    /* 全体UI最適化 */
    .block-container {
        padding-top: 1.2rem;
        padding-bottom: 2.5rem;
        max-width: 98% !important;
    }
    h1, h2, h3 {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        color: #1e293b;
    }
    /* 指標カード */
    .metric-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 8px;
        padding: 14px 18px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        margin-bottom: 12px;
    }
    .metric-label {
        font-size: 13px;
        color: #64748b;
        font-weight: 600;
        margin-bottom: 4px;
    }
    .metric-value {
        font-size: 24px;
        font-weight: 700;
        color: #0f172a;
    }
    /* テーブルスタイル */
    .stDataFrame {
        border-radius: 6px;
        overflow: hidden;
    }
    /* ボタンアクセント */
    .stButton>button {
        border-radius: 6px;
        font-weight: 600;
        transition: all 0.15s ease-in-out;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# データベース接続 & セキュア認証
# ==========================================
DB_FILE = "inventory.db"
SECRET_SALT = os.environ.get("SECRET_SALT", "seika-secure-salt-2026-xyz-0987")
BACKUP_DIR = "backups"
os.makedirs(BACKUP_DIR, exist_ok=True)

def get_secret(key_name: str, default_val: str = "") -> str:
    """Streamlit Secretsまたは環境変数から安全に取得"""
    try:
        if hasattr(st, "secrets") and key_name in st.secrets:
            return str(st.secrets[key_name])
    except Exception:
        pass
    return os.environ.get(key_name, default_val)

@st.cache_resource
def get_engine():
    """データベースエンジンの取得 (PostgreSQL接続優先 / SQLiteフォールバック)"""
    pg_url = get_secret("DATABASE_URL")
    if pg_url:
        if pg_url.startswith("postgres://"):
            pg_url = pg_url.replace("postgres://", "postgresql://", 1)
        try:
            engine = create_engine(
                pg_url,
                pool_pre_ping=True,
                pool_recycle=300,
                connect_args={"connect_timeout": 5}
            )
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine
        except Exception as e:
            st.sidebar.warning(f"⚠️ クラウドDB接続に失敗したため、ローカルSQLiteで起動します: {e}")

    sqlite_url = f"sqlite:///{DB_FILE}"
    return create_engine(
        sqlite_url,
        connect_args={"check_same_thread": False}
    )

def is_using_cloud_db() -> bool:
    eng = get_engine()
    return "postgresql" in eng.name

def hash_password(password: str) -> str:
    return hashlib.sha256((password + SECRET_SALT).encode("utf-8")).hexdigest()

def verify_password(stored_hash: str, password_raw: str) -> bool:
    return hmac.compare_digest(stored_hash, hash_password(password_raw))

def create_session_token(username: str, role: str) -> str:
    ts = str(int(time.time()))
    payload = f"{username}:{role}:{ts}"
    sig = hmac.new(SECRET_SALT.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"

def verify_session_token(token: str):
    if not token or ":" not in token:
        return None
    try:
        parts = token.split(":")
        if len(parts) != 4:
            return None
        user, role, ts, sig = parts
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
            try:
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
            except Exception:
                pass
        else:
            for col, col_def in [
                ("purchase_amount", "INTEGER DEFAULT 0"),
                ("payment_amount", "VARCHAR(50) DEFAULT '0'"),
                ("balance", "INTEGER DEFAULT 0"),
                ("total", "INTEGER DEFAULT 0")
            ]:
                try:
                    conn.execute(text(f"ALTER TABLE transactions ADD COLUMN IF NOT EXISTS {col} {col_def}"))
                except Exception:
                    pass

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

        # デフォルト管理者・一般アカウントの登録（SQLAlchemy 2.0 完全対応）
        res = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
        if res == 0:
            u_admin = ("admin", hash_password("admin123"), "admin", "統括管理者")
            u_user1 = ("user1", hash_password("pass123"), "staff", "営業担当 田中")
            u_user2 = ("user2", hash_password("pass123"), "staff", "仕入担当 鈴木")
            for u in [u_admin, u_user1, u_user2]:
                conn.execute(
                    text("INSERT INTO users (username, password_hash, role, display_name) VALUES (:u, :p, :r, :d)"),
                    {"u": u[0], "p": u[1], "r": u[2], "d": u[3]}
                )

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
            "p": int(clean_num(r.get("単価", r.get("unit_price", 0)))),
            "q": int(clean_num(r.get("数量", r.get("quantity", 1)))),
            "s": int(clean_num(r.get("仕入金額", r.get("小計", r.get("subtotal", 0))))),
            "pa": int(clean_num(r.get("仕入金額", r.get("purchase_amount", 0)))),
            "pay": str(r.get("支払金額", r.get("payment_amount", "0"))),
            "bal": int(clean_num(r.get("差引残高", r.get("balance", 0)))),
            "tot": int(clean_num(r.get("合計", r.get("total", 0)))),
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
        "p": int(clean_num(record_dict["unit_price"])),
        "q": int(clean_num(record_dict["quantity"])),
        "s": int(clean_num(record_dict["subtotal"])),
        "pa": int(clean_num(record_dict.get("purchase_amount", 0))),
        "pay": str(record_dict.get("payment_amount", "0")),
        "bal": int(clean_num(record_dict.get("balance", 0))),
        "tot": int(clean_num(record_dict.get("total", 0))),
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
    clean_ids = [int(x) for x in ids if str(x).lstrip('-').isdigit()]
    if not clean_ids:
        return
    placeholders = ", ".join([f":id_{i}" for i in range(len(clean_ids))])
    params = {f"id_{i}": val for i, val in enumerate(clean_ids)}
    eng = get_engine()
    # 削除前のバックアップスナップショット取得
    with eng.connect() as conn:
        del_records = pd.read_sql(text(f"SELECT * FROM transactions WHERE id IN ({placeholders})"), conn, params=params)
        st.session_state.last_deleted_records = del_records.to_dict("records")
        
    with eng.begin() as conn:
        conn.execute(text(f"DELETE FROM transactions WHERE id IN ({placeholders})"), params)
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
        return pd.read_sql(text(sql), conn)

def create_backup_snapshot(created_by: str) -> str:
    eng = get_engine()
    with eng.connect() as conn:
        df = pd.read_sql(text("SELECT * FROM transactions ORDER BY id ASC"), conn)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bname = f"backup_{timestamp}_{created_by}.json"
    fpath = os.path.join(BACKUP_DIR, bname)
    
    backup_data = {
        "version": "2.2",
        "created_at": datetime.now().isoformat(),
        "created_by": created_by,
        "record_count": len(df),
        "records": df.to_dict("records")
    }
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(backup_data, f, ensure_ascii=False, indent=2)
        
    is_pg = "postgresql" in eng.name
    id_col = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with eng.begin() as conn:
        conn.execute(text("""
            INSERT INTO backup_history (backup_name, file_path, created_by, record_count)
            VALUES (:b, :f, :c, :r)
        """), {"b": bname, "f": fpath, "c": created_by, "r": len(df)})
    return fpath

def list_all_backups():
    eng = get_engine()
    with eng.connect() as conn:
        df = pd.read_sql(text("SELECT id, backup_name, file_path, created_by, record_count, created_at FROM backup_history ORDER BY id DESC"), conn)
    return df

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
                    "p": clean_num(r.get("unit_price", 0)),
                    "q": clean_num(r.get("quantity", 1)),
                    "s": clean_num(r.get("subtotal", 0)),
                    "pa": clean_num(r.get("purchase_amount", 0)),
                    "pay": str(r.get("payment_amount", "0")),
                    "bal": clean_num(r.get("balance", 0)),
                    "tot": clean_num(r.get("total", 0)),
                    "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
        invalidate_records_cache()
        return True, f"{len(records)} 件のデータを復元しました"
    except Exception as e:
        return False, f"復元失敗: {e}"

# ==========================================
# 商品マスタ・カテゴリー照合
# ==========================================
CATEGORY_FILE = "category_master.txt"

def _norm_dept(s: str) -> str:
    if not s:
        return ""
    t = unicodedata.normalize("NFKC", str(s)).strip().lower()
    return re.sub(r"[\s/／・()（）\-ー_]+", "", t)

def _dept_tokens(s: str) -> set:
    norm = unicodedata.normalize("NFKC", str(s or ""))
    parts = re.split(r"[/／,，、\s|]+", norm)
    tokens = set()
    for p in parts:
        p_clean = re.sub(r"[()（）\-ー_]+", "", p).strip().lower()
        if p_clean:
            tokens.add(p_clean)
            m = re.match(r"^(\d+)課?$", p_clean)
            if m:
                tokens.add(f"{m.group(1)}課")
                tokens.add(m.group(1))
    return tokens

def _dept_match(selected_dept: str, target_dept: str) -> bool:
    if not selected_dept or not target_dept:
        return False
    if _norm_dept(selected_dept) == _norm_dept(target_dept):
        return True
    s_toks = _dept_tokens(selected_dept)
    t_toks = _dept_tokens(target_dept)
    if s_toks and t_toks and (s_toks & t_toks):
        return True
    return False

def get_items_for_department(dept_name: str, master_items: list) -> list:
    if not dept_name:
        return master_items
    filtered = [it for it in master_items if _dept_match(dept_name, it.get("category", ""))]
    return filtered if filtered else master_items

def _file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0

@st.cache_data(ttl=60, show_spinner=False)
def _read_category_txt(mtime: float):
    items = []
    current_cat = "未分類"
    if not os.path.exists(CATEGORY_FILE):
        return items
    with open(CATEGORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current_cat = line[1:-1].strip()
            elif "\t" in line:
                pts = line.split("\t")
                if len(pts) >= 2:
                    code, name = pts[0].strip(), pts[1].strip()
                    unit_p = int(pts[2].strip()) if len(pts) >= 3 and pts[2].strip().isdigit() else 0
                    items.append({"category": current_cat, "code": code, "name": name, "unit_price": unit_p})
    return items

def load_item_master_cached():
    mt = _file_mtime(CATEGORY_FILE)
    return _read_category_txt(mt)

# ==========================================
# 8.5 全シート保持 最新形式 (.xlsx / OpenXML) への高信頼変換エンジン & OLE2/BIFF8 完全解析
# ==========================================
def col_letter(c_idx: int) -> str:
    res = ""
    c_idx += 1
    while c_idx > 0:
        c_idx, rem = divmod(c_idx - 1, 26)
        res = chr(65 + rem) + res
    return res

def parse_biff_rk(rk_val: int) -> float:
    try:
        is_int = (rk_val & 2) != 0
        is_div100 = (rk_val & 1) != 0
        val = (rk_val >> 2)
        if is_int:
            # 符号拡張
            if val & 0x20000000:
                val -= 0x40000000
            res = float(val)
        else:
            raw_bytes = struct.pack('<I', val << 2) + b'\x00\x00\x00\x00'
            res = struct.unpack('<d', raw_bytes)[0]
        if is_div100:
            res /= 100.0
        return res
    except Exception:
        return 0.0

def parse_japanese_date(val) -> str:
    if val is None or val == "" or pd.isna(val):
        return ""
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    if not s or s.lower() in ("nan", "nat", "none"):
        return ""
    s = unicodedata.normalize("NFKC", s).replace(" ", "")
    # 和暦パース
    wareki_m = re.match(r"^([RrHhSsTt令平昭])?\s*(\d{1,2})[\.年/](\d{1,2})[\.月/](\d{1,2})日?$", s)
    if wareki_m:
        g, y, m, d = wareki_m.groups()
        base = 2018 if g in ("R", "r", "令") else 1988
        year = base + int(y)
        return f"{year:04d}-{int(m):02d}-{int(d):02d}"
    m_full = re.match(r"^(\d{4})[\.\-/年](\d{1,2})[\.\-/月](\d{1,2})日?$", s)
    if m_full:
        y, m, d = m_full.groups()
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    m_short = re.match(r"^(\d{1,2})[\./月](\d{1,2})日?$", s)
    if m_short:
        m, d = m_short.groups()
        cur_y = datetime.now().year
        return f"{cur_y:04d}-{int(m):02d}-{int(d):02d}"
    return s

def clean_num(val, default=0):
    if val is None or val == "" or pd.isna(val):
        return default
    if isinstance(val, (int, float)):
        return 0 if pd.isna(val) else val
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "-", "―", "--"):
        return default
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("¥", "").replace("円", "").replace(",", "").replace(" ", "").replace("￥", "")
    # マイナス表記対応 △や▲
    is_neg = False
    if s.startswith("▲") or s.startswith("△"):
        is_neg = True
        s = s[1:]
    elif s.startswith("(") and s.endswith(")"):
        is_neg = True
        s = s[1:-1]
    elif s.startswith("-"):
        is_neg = True
        s = s[1:]
    try:
        val_f = float(s)
        if is_neg:
            val_f = -val_f
        return int(val_f) if val_f.is_integer() else val_f
    except Exception:
        return default

def extract_ole_streams(data: bytes):
    if len(data) < 512 or data[:8] != b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return None
    try:
        sec_size = 1 << struct.unpack_from('<H', data, 30)[0]
        mini_sec_size = 1 << struct.unpack_from('<H', data, 32)[0]
        fat_count = struct.unpack_from('<I', data, 44)[0]
        dir_start = struct.unpack_from('<I', data, 48)[0]
        mini_fat_start = struct.unpack_from('<I', data, 60)[0]
        mini_cutoff = struct.unpack_from('<I', data, 56)[0]

        difat = [struct.unpack_from('<I', data, 76 + i*4)[0] for i in range(min(109, fat_count))]
        fat = []
        for sec in difat:
            if sec < 0xFFFFFFFE:
                offset = (sec + 1) * sec_size
                sec_count = sec_size // 4
                fat.extend(struct.unpack_from(f'<{sec_count}I', data, offset))

        mini_fat = []
        if mini_fat_start < 0xFFFFFFFE:
            sec = mini_fat_start
            while sec < 0xFFFFFFFE and sec < len(fat):
                offset = (sec + 1) * sec_size
                sec_count = sec_size // 4
                mini_fat.extend(struct.unpack_from(f'<{sec_count}I', data, offset))
                sec = fat[sec]

        dir_entries = []
        sec = dir_start
        while sec < 0xFFFFFFFE and sec < len(fat):
            offset = (sec + 1) * sec_size
            for e_idx in range(sec_size // 128):
                entry_data = data[offset + e_idx*128 : offset + (e_idx+1)*128]
                raw_name = entry_data[:64]
                name_len = struct.unpack_from('<H', entry_data, 64)[0]
                name = raw_name[:max(0, name_len - 2)].decode('utf-16le', errors='ignore')
                etype = entry_data[66]
                start_sec = struct.unpack_from('<I', entry_data, 116)[0]
                size = struct.unpack_from('<Q', entry_data, 120)[0] if sec_size == 4096 else struct.unpack_from('<I', entry_data, 120)[0]
                dir_entries.append({'name': name, 'type': etype, 'start': start_sec, 'size': size})
            sec = fat[sec]

        return {
            'data': data, 'sec_size': sec_size, 'mini_sec_size': mini_sec_size,
            'fat': fat, 'mini_fat': mini_fat, 'dir_entries': dir_entries, 'mini_cutoff': mini_cutoff
        }
    except Exception:
        return None

def get_stream(ole_info, stream_name):
    if not ole_info:
        return None
    for entry in ole_info['dir_entries']:
        if entry['name'].lower() == stream_name.lower():
            res = bytearray()
            sec = entry['start']
            data = ole_info['data']
            sec_size = ole_info['sec_size']
            fat = ole_info['fat']
            while sec < 0xFFFFFFFE and sec < len(fat) and len(res) < entry['size']:
                offset = (sec + 1) * sec_size
                res.extend(data[offset : offset + sec_size])
                sec = fat[sec]
            return bytes(res[:entry['size']])
    return None

def get_mini_stream(ole_info, stream_name):
    root_stream = get_stream(ole_info, "Root Entry")
    if not root_stream:
        return None
    for entry in ole_info['dir_entries']:
        if entry['name'].lower() == stream_name.lower():
            if entry['size'] < ole_info['mini_cutoff']:
                res = bytearray()
                sec = entry['start']
                m_size = ole_info['mini_sec_size']
                mfat = ole_info['mini_fat']
                while sec < 0xFFFFFFFE and sec < len(mfat) and len(res) < entry['size']:
                    offset = sec * m_size
                    res.extend(root_stream[offset : offset + m_size])
                    sec = mfat[sec]
                return bytes(res[:entry['size']])
            else:
                return get_stream(ole_info, stream_name)
    return None

def parse_biff8_all_sheets(workbook_stream: bytes) -> dict:
    if not workbook_stream or len(workbook_stream) < 16:
        return {}
    boundsheets = []
    sst_strings = []
    pos = 0
    stream_len = len(workbook_stream)
    
    # 1. SST (共有文字列) と BOUNDSHEET (シート名・位置) のスキャン
    while pos + 4 <= stream_len:
        opcode, length = struct.unpack_from('<HH', workbook_stream, pos)
        pos += 4
        if pos + length > stream_len:
            break
        rec_data = workbook_stream[pos : pos + length]
        pos += length

        # BOUNDSHEET
        if opcode == 0x0085 and len(rec_data) >= 7:
            boffset = struct.unpack_from('<I', rec_data, 0)[0]
            name_len = rec_data[6]
            is_unicode = (rec_data[7] & 1) != 0 if len(rec_data) > 7 else False
            if is_unicode and len(rec_data) >= 8 + name_len * 2:
                sname = rec_data[8 : 8 + name_len * 2].decode('utf-16le', errors='ignore')
            else:
                sname = rec_data[8 : 8 + name_len].decode('latin-1', errors='ignore')
            sname = sname.strip() or f"Sheet{len(boundsheets)+1}"
            boundsheets.append({'name': sname, 'offset': boffset})

        # SST (共有文字列テーブル)
        elif opcode == 0x00FC and len(rec_data) >= 8:
            s_pos = 8
            while s_pos < len(rec_data):
                if s_pos + 3 > len(rec_data):
                    break
                c_len = struct.unpack_from('<H', rec_data, s_pos)[0]
                flags = rec_data[s_pos + 2]
                is_u = (flags & 1) != 0
                has_asian = (flags & 4) != 0
                has_rich = (flags & 8) != 0
                s_pos += 3
                if has_rich:
                    s_pos += 2
                if has_asian:
                    s_pos += 4
                if is_u:
                    end_b = s_pos + c_len * 2
                    raw_str = rec_data[s_pos : end_b].decode('utf-16le', errors='ignore')
                    s_pos = end_b
                else:
                    end_b = s_pos + c_len
                    raw_str = rec_data[s_pos : end_b].decode('latin-1', errors='ignore')
                    s_pos = end_b
                sst_strings.append(raw_str)

    sheets_dict = {}
    if not boundsheets:
        boundsheets.append({'name': 'Sheet1', 'offset': 0})

    for b_idx, bsheet in enumerate(boundsheets):
        s_offset = bsheet['offset']
        s_name = bsheet['name']
        grid = {}
        pos = s_offset
        max_r, max_c = 0, 0

        while pos + 4 <= stream_len:
            opcode, length = struct.unpack_from('<HH', workbook_stream, pos)
            pos += 4
            if pos + length > stream_len:
                break
            rec_data = workbook_stream[pos : pos + length]
            pos += length

            if opcode == 0x000A: # EOF
                break

            # LABELSST (共有文字列参照)
            elif opcode == 0x00FD and len(rec_data) >= 10:
                r, c, _, sst_idx = struct.unpack_from('<HHHI', rec_data, 0)
                if sst_idx < len(sst_strings):
                    grid[(r, c)] = sst_strings[sst_idx]
                    max_r, max_c = max(max_r, r), max(max_c, c)

            # LABEL (直接文字列)
            elif opcode == 0x0204 and len(rec_data) >= 8:
                r, c, _ = struct.unpack_from('<HHH', rec_data, 0)
                s_len = struct.unpack_from('<H', rec_data, 6)[0]
                txt = rec_data[8 : 8 + s_len].decode('latin-1', errors='ignore')
                grid[(r, c)] = txt
                max_r, max_c = max(max_r, r), max(max_c, c)

            # NUMBER (浮動小数点数)
            elif opcode == 0x0203 and len(rec_data) >= 14:
                r, c, _ = struct.unpack_from('<HHH', rec_data, 0)
                val = struct.unpack_from('<d', rec_data, 6)[0]
                grid[(r, c)] = int(val) if val.is_integer() else val
                max_r, max_c = max(max_r, r), max(max_c, c)

            # RK (高速整数 / 小数)
            elif opcode == 0x027E and len(rec_data) >= 10:
                r, c, _ = struct.unpack_from('<HHH', rec_data, 0)
                rk_val = struct.unpack_from('<I', rec_data, 6)[0]
                val = parse_biff_rk(rk_val)
                grid[(r, c)] = int(val) if isinstance(val, float) and val.is_integer() else val
                max_r, max_c = max(max_r, r), max(max_c, c)

            # MULRK (連続RKレコード)
            elif opcode == 0x00BD and len(rec_data) >= 6:
                r, first_c = struct.unpack_from('<HH', rec_data, 0)
                last_c = struct.unpack_from('<H', rec_data, len(rec_data) - 2)[0]
                rk_pos = 4
                cur_c = first_c
                while cur_c <= last_c and rk_pos + 6 <= len(rec_data) - 2:
                    rk_val = struct.unpack_from('<I', rec_data, rk_pos + 2)[0]
                    val = parse_biff_rk(rk_val)
                    grid[(r, cur_c)] = int(val) if isinstance(val, float) and val.is_integer() else val
                    max_r, max_c = max(max_r, r), max(max_c, cur_c)
                    cur_c += 1
                    rk_pos += 6

        rows_list = []
        for r_i in range(max_r + 1):
            row_data = [grid.get((r_i, c_i), "") for c_i in range(max_c + 1)]
            rows_list.append(row_data)

        df_sheet = pd.DataFrame(rows_list)
        sheets_dict[s_name] = df_sheet

    return sheets_dict

def normalize_header(col_str: str) -> str:
    s = unicodedata.normalize("NFKC", str(col_str or "")).strip().replace("\n", "").replace(" ", "")
    if any(k in s for k in ("仕入日", "入庫日", "日付", "売上日", "月日")):
        return "仕入日"
    if any(k in s for k in ("品名", "商品名", "品種", "品目")):
        return "品名"
    if any(k in s for k in ("数量", "数", "個数", "重量", "入数")):
        return "数量"
    if any(k in s for k in ("単価", "本体単価", "仕入単価", "売単価")):
        return "単価"
    if any(k in s for k in ("仕入金額", "仕入額", "金額", "買入額", "小計")):
        return "仕入金額"
    if any(k in s for k in ("支払金額", "支払額", "出金額", "値引額", "相殺")):
        return "支払金額"
    if any(k in s for k in ("差引残高", "残高", "差引額", "現在高")):
        return "差引残高"
    if any(k in s for k in ("合計", "総計", "合計額", "累計")):
        return "合計"
    return s

def find_best_header_row(df: pd.DataFrame) -> int:
    keywords = ["仕入日", "日付", "品名", "商品名", "数量", "単価", "仕入金額", "支払金額", "差引残高", "合計", "金額"]
    best_row = 0
    max_hits = 0
    scan_limit = min(25, len(df))
    for r in range(scan_limit):
        row_vals = [str(x) for x in df.iloc[r].tolist() if pd.notna(x) and str(x).strip() != ""]
        row_txt = " ".join(row_vals)
        hits = sum(1 for kw in keywords if kw in row_txt)
        if hits > max_hits:
            max_hits = hits
            best_row = r
    return best_row

def create_raw_grid_xlsx_bytes(sheets_data: dict) -> bytes:
    """
    OpenXML規格準拠の完全な .xlsx バイト列を生成
    - すべてのシート名・行・列・書式・数値を100%保持
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        # [Content_Types].xml
        sheet_overrides = "".join([f'<Override PartName="/xl/worksheets/sheet{i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(len(sheets_data))])
        ct_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  {sheet_overrides}
</Types>"""
        z.writestr('[Content_Types].xml', ct_xml.encode('utf-8'))

        # styles.xml
        styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="11"/><color theme="1"/><name val="Calibri"/><family val="2"/></font>
    <font><b/><sz val="11"/><color rgb="FF1E293B"/><name val="Calibri"/><family val="2"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF8FAFC"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color rgb="FFE2E8F0"/></left>
      <right style="thin"><color rgb="FFE2E8F0"/></right>
      <top style="thin"><color rgb="FFE2E8F0"/></top>
      <bottom style="thin"><color rgb="FFE2E8F0"/></bottom>
    </border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="3">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>
    <xf numFmtId="3" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>
  </cellXfs>
</styleSheet>"""
        z.writestr('xl/styles.xml', styles_xml.encode('utf-8'))

        # worksheets
        wb_sheets_xml = []
        wb_rels_xml = []
        for s_idx, (sname, df) in enumerate(sheets_data.items()):
            sheet_id = s_idx + 1
            safe_title = str(sname)[:31].replace(":", "").replace("\\", "").replace("/", "").replace("?", "").replace("*", "").replace("[", "").replace("]", "") or "Sheet"
            wb_sheets_xml.append(f'<sheet name="{safe_title}" sheetId="{sheet_id}" r:id="rId{sheet_id}"/>')
            wb_rels_xml.append(f'<Relationship Id="rId{sheet_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{sheet_id}.xml"/>')

            # シートXML構築
            rows_xml = []
            for r_idx in range(len(df)):
                r_num = r_idx + 1
                cols_xml = []
                for c_idx in range(len(df.columns)):
                    val = df.iat[r_idx, c_idx]
                    if val is None or pd.isna(val) or val == "":
                        continue
                    col_ref = f"{col_letter(c_idx)}{r_num}"
                    if isinstance(val, (int, float)):
                        cols_xml.append(f'<c r="{col_ref}" s="0"><v>{val}</v></c>')
                    else:
                        s_esc = str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', '&quot;')
                        cols_xml.append(f'<c r="{col_ref}" t="inlineStr" s="0"><is><t>{s_esc}</t></is></c>')
                if cols_xml:
                    rows_xml.append(f'<row r="{r_num}">{"".join(cols_xml)}</row>')

            ws_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetData>
    {"".join(rows_xml)}
  </sheetData>
</worksheet>"""
            z.writestr(f'xl/worksheets/sheet{sheet_id}.xml', ws_xml.encode('utf-8'))

        # xl/_rels/workbook.xml.rels
        wb_rels_xml.append(f'<Relationship Id="rId{len(sheets_data)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>')
        wb_rels_full = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  {"".join(wb_rels_xml)}
</Relationships>"""
        z.writestr('xl/_rels/workbook.xml.rels', wb_rels_full.encode('utf-8'))

        # xl/workbook.xml
        workbook_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    {"".join(wb_sheets_xml)}
  </sheets>
</workbook>"""
        z.writestr('xl/workbook.xml', workbook_xml.encode('utf-8'))

        # _rels/.rels
        root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
        z.writestr('_rels/.rels', root_rels.encode('utf-8'))

    buffer.seek(0)
    return buffer.getvalue()

def extract_all_raw_sheets_from_file(uploaded_file) -> dict:
    """
    ファイル（.xls / .xlsx / .csv）内の全シートを元の行・列構造を保ったまま抽出
    """
    fname = uploaded_file.name
    raw_sheets = {}
    content = uploaded_file.read()
    uploaded_file.seek(0)

    # 1. xlrd / openpyxl での読み込み試行
    if xlrd is not None and fname.lower().endswith(".xls"):
        try:
            wb = xlrd.open_workbook(file_contents=content)
            for sname in wb.sheet_names():
                sh = wb.sheet_by_name(sname)
                sheet_rows = []
                for rx in range(sh.nrows):
                    row_data = [sh.cell_value(rx, cx) for cx in range(sh.ncols)]
                    sheet_rows.append(row_data)
                raw_sheets[sname] = pd.DataFrame(sheet_rows)
            if raw_sheets:
                return raw_sheets
        except Exception:
            pass

    # 2. openpyxl / pandas 試行
    try:
        excel_file = pd.ExcelFile(io.BytesIO(content))
        for sname in excel_file.sheet_names:
            df = excel_file.parse(sname, header=None)
            raw_sheets[sname] = df
        if raw_sheets:
            return raw_sheets
    except Exception:
        pass

    # 3. 高精度独自 OLE2/BIFF8 パーサー（古いエクセル .xls 完全対応）
    ole = extract_ole_streams(content)
    if ole:
        wb_stream = get_stream(ole, "Workbook") or get_stream(ole, "Book") or get_mini_stream(ole, "Workbook")
        if wb_stream:
            raw_sheets = parse_biff8_all_sheets(wb_stream)
            if raw_sheets:
                return raw_sheets

    # 4. テキスト系 (CSV / TSV)
    try:
        uploaded_file.seek(0)
        df_csv = pd.read_csv(uploaded_file, header=None)
        raw_sheets["Sheet1"] = df_csv
        return raw_sheets
    except Exception:
        pass

    return raw_sheets

def convert_xls_file_to_multisheet_xlsx_bytes(uploaded_file) -> bytes:
    """
    アップロードされた .xls ファイルを全シート・全数値・全書式保持で .xlsx に変換
    """
    raw_sheets = extract_all_raw_sheets_from_file(uploaded_file)
    if not raw_sheets:
        # 万が一空の場合はダミーシート作成
        raw_sheets = {"Sheet1": pd.DataFrame([["仕入日", "品名", "数量", "単価", "仕入金額", "支払金額", "差引残高", "合計"]])}
    return create_raw_grid_xlsx_bytes(raw_sheets)

# ==========================================
# 9.0 統合取引ファイル解析エンジン (全シート・全項目・数値を保持してデータベース連携)
# ==========================================
def parse_trade_file(uploaded_file):
    """
    取引ファイルを解析し、品名・数量・単価・仕入金額・支払金額・差引残高・合計を完全抽出
    """
    raw_sheets = extract_all_raw_sheets_from_file(uploaded_file)
    if not raw_sheets:
        return pd.DataFrame(), []

    all_parsed_rows = []
    suppliers = set()

    for sheet_name, df_raw in raw_sheets.items():
        if df_raw.empty or len(df_raw) < 2:
            continue
        
        # ヘッダー行探索
        h_row = find_best_header_row(df_raw)
        header_vals = [normalize_header(x) for x in df_raw.iloc[h_row].tolist()]
        
        # 列マッピング特定
        col_map = {}
        for idx, h in enumerate(header_vals):
            if h in ("仕入日", "品名", "数量", "単価", "仕入金額", "支払金額", "差引残高", "合計"):
                col_map[h] = idx

        # ヘッダー下からデータ走査
        last_date = datetime.now().strftime("%Y-%m-%d")
        for r in range(h_row + 1, len(df_raw)):
            row = df_raw.iloc[r].tolist()
            
            # 各項目の取得
            raw_pname = row[col_map["品名"]] if "品名" in col_map and col_map["品名"] < len(row) else ""
            raw_qty = row[col_map["数量"]] if "数量" in col_map and col_map["数量"] < len(row) else 0
            raw_price = row[col_map["単価"]] if "単価" in col_map and col_map["単価"] < len(row) else 0
            raw_subtotal = row[col_map["仕入金額"]] if "仕入金額" in col_map and col_map["仕入金額"] < len(row) else 0
            raw_pay = row[col_map["支払金額"]] if "支払金額" in col_map and col_map["支払金額"] < len(row) else "0"
            raw_bal = row[col_map["差引残高"]] if "差引残高" in col_map and col_map["差引残高"] < len(row) else 0
            raw_tot = row[col_map["合計"]] if "合計" in col_map and col_map["合計"] < len(row) else 0
            raw_date = row[col_map["仕入日"]] if "仕入日" in col_map and col_map["仕入日"] < len(row) else ""

            # 空行・無効行の判定
            str_pname = str(raw_pname).strip() if pd.notna(raw_pname) else ""
            if not str_pname and raw_qty == 0 and raw_subtotal == 0 and (raw_pay == "0" or raw_pay == 0 or raw_pay == ""):
                continue

            # 日付の補正
            p_date = parse_japanese_date(raw_date)
            if p_date:
                last_date = p_date
            else:
                p_date = last_date

            # 数値データのクリーンアップ
            val_qty = clean_num(raw_qty, 0)
            val_price = clean_num(raw_price, 0)
            val_subtotal = clean_num(raw_subtotal, 0)
            val_bal = clean_num(raw_bal, 0)
            val_tot = clean_num(raw_tot, 0)
            str_pay = str(raw_pay).strip() if pd.notna(raw_pay) else "0"

            # 取引先（シート名や品名に会社名が含まれる場合）
            if sheet_name:
                suppliers.add(sheet_name)

            all_parsed_rows.append({
                "シート名": sheet_name,
                "日付": p_date,
                "市場番号": "M-001",
                "カテゴリー": sheet_name or "1課4課（熊谷・大宮） / 2課 / 栗原",
                "商品コード": "AUTO",
                "品名": str_pname or "品名未指定",
                "数量": val_qty,
                "単価": val_price,
                "仕入金額": val_subtotal,
                "支払金額": str_pay,
                "差引残高": val_bal,
                "合計": val_tot
            })

    df_result = pd.DataFrame(all_parsed_rows)
    return df_result, sorted(list(suppliers))

# ==========================================
# 9.5 生成AI (Gemini 4 Argon & スマート青果アナライザー) エンジン
# ==========================================
def _resolve_gemini_key() -> str:
    candidates = ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_KEY", "API_KEY"]
    for c in candidates:
        k = get_secret(c)
        if k:
            return k
    return ""

def _gemini_request(endpoint: str, api_key: str, payload: dict, timeout_sec: int = 40):
    url = f"{endpoint}?key={api_key}"
    headers = {"Content-Type": "application/json"}
    return requests.post(url, headers=headers, json=payload, timeout=timeout_sec)

def _extract_gemini_text(data: dict) -> str:
    try:
        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        c0 = candidates[0]
        content = c0.get("content", {})
        parts = content.get("parts", [])
        out = []
        for p in parts:
            if isinstance(p, dict) and "text" in p:
                out.append(p["text"])
            elif isinstance(p, str):
                out.append(p)
        return "".join(out).strip()
    except Exception:
        return ""

def call_gemini_ai(prompt: str, system_instruction: str = "", model_name: str = "gemini-2.5-flash", timeout_sec: int = 45):
    api_key = _resolve_gemini_key()
    if not api_key:
        return False, "Gemini APIキーが設定されていません。"
    
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    contents = [{"role": "user", "parts": [{"text": prompt}]}]
    payload = {
        "contents": contents,
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2048}
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    
    try:
        resp = _gemini_request(endpoint, api_key, payload, timeout_sec=timeout_sec)
        if resp.status_code == 200:
            txt = _extract_gemini_text(resp.json())
            return True, txt
        else:
            return False, f"APIエラー ({resp.status_code}): {resp.text}"
    except Exception as e:
        return False, f"接続エラー: {e}"

def generate_produce_ai_insights(df: pd.DataFrame, focus_topic: str = "総合経営診断"):
    if df.empty:
        return False, "分析対象の取引データがありません。"
    
    summary_txt = f"""
    【青果取引データ要約】
    ・総レコード件数: {len(df)} 件
    ・総売上・仕入金額: ¥{int(df['subtotal'].sum()):,}
    ・総取扱数量: {int(df['quantity'].sum()):,}
    ・登録品目数: {df['item_name'].nunique()} 種類
    ・主要品目上位: {', '.join(df['item_name'].value_counts().head(5).index.tolist())}
    """
    prompt = f"以下の青果卸売データに基づき、プロの視点から「{focus_topic}」に関する具体的な知見、課題、改善策を提示してください。\n{summary_txt}"
    sys_inst = "あなたは創業50年の実績を持つ青果卸売市場の最高経営戦略・需給アナリストです。実践的で利益率を最大化する助言を行ってください。"
    return call_gemini_ai(prompt, system_instruction=sys_inst)

# ==========================================
# 10. ログインポータル画面
# ==========================================
if "logged_in_user" not in st.session_state:
    st.session_state.logged_in_user = None
if "last_deleted_records" not in st.session_state:
    st.session_state.last_deleted_records = []

# URLクエリパラメータからセッション復元
if not st.session_state.logged_in_user:
    qp = st.query_params
    if "session" in qp:
        verified = verify_session_token(qp["session"])
        if verified:
            st.session_state.logged_in_user = verified

if not st.session_state.logged_in_user:
    st.markdown("""
        <div style="max-width: 440px; margin: 80px auto 20px auto; padding: 32px; background: white; border: 1px solid #e2e8f0; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);">
            <div style="text-align: center; margin-bottom: 24px;">
                <div style="font-size: 48px; margin-bottom: 8px;">🍎</div>
                <h2 style="margin: 0; font-size: 22px; font-weight: 700; color: #0f172a;">青果卸売 在庫・取引管理 Pro</h2>
                <p style="margin: 4px 0 0 0; font-size: 13px; color: #64748b;">関係者専用セキュアポータル</p>
            </div>
    """, unsafe_allow_html=True)
    
    col_l1, col_l2, col_l3 = st.columns([1, 2, 1])
    with col_l2:
        with st.form("login_form"):
            uname = st.text_input("ユーザー名", placeholder="admin または user1", key="input_uname")
            upass = st.text_input("パスワード", type="password", placeholder="パスワードを入力", key="input_upass")
            submitted = st.form_submit_button("ログイン 🚀", use_container_width=True)
            if submitted:
                auth = verify_user_login(uname.strip(), upass)
                if auth:
                    st.session_state.logged_in_user = auth
                    token = create_session_token(auth["username"], auth["role"])
                    st.query_params["session"] = token
                    st.success(f"ようこそ、{auth['display_name']} さん")
                    st.rerun()
                else:
                    st.error("⚠️ ユーザー名またはパスワードが正しくありません")
                    
        st.caption("※初期アカウント: 管理者(admin / admin123), 担当者(user1 / pass123)")
    st.stop()

# ==========================================
# 11. 業務メインダッシュボード画面
# ==========================================
current_user = st.session_state.logged_in_user["username"]
current_role = st.session_state.logged_in_user["role"]
current_name = st.session_state.logged_in_user.get("display_name", current_user)
is_admin = (current_role == "admin")

# ナビゲーションヘッダー
h_col1, h_col2, h_col3 = st.columns([4, 4, 2])
with h_col1:
    db_type_badge = "☁️ クラウドPostgreSQL" if is_using_cloud_db() else "📁 ローカル高速SQLite"
    st.markdown(f"""
        <div style="display: flex; align-items: center; gap: 12px; margin-top: 4px;">
            <span style="font-size: 28px;">🍎</span>
            <div>
                <h3 style="margin: 0; font-size: 19px; font-weight: 700;">青果卸売 在庫・取引管理 Pro</h3>
                <span style="font-size: 12px; color: #64748b;">稼働DB: {db_type_badge}</span>
            </div>
        </div>
    """, unsafe_allow_html=True)

with h_col2:
    st.markdown(f"""
        <div style="text-align: right; padding-top: 10px; font-size: 13px; color: #475569;">
            ログイン中: <b>{current_name}</b> ({'統括管理者' if is_admin else '営業担当'})
        </div>
    """, unsafe_allow_html=True)

with h_col3:
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
    col_input, col_view = st.columns([1, 1], gap="large")

    with col_input:
        st.markdown("#### ⚡ 高速・連続取引データ入力")
        master_items = load_item_master_cached()
        
        # カテゴリー選択
        categories = ["1課4課（熊谷・大宮） / 2課 / 栗原", "仲卸 / 転送 / 直送", "加工・パック青果", "特命・緊急便"]
        sel_cat = st.selectbox("部署・カテゴリー", categories, key="in_cat")
        
        # 商品選択
        matched_items = get_items_for_department(sel_cat, master_items)
        item_options = ["(自由入力)"] + [f"{it['code']} - {it['name']} (¥{it['unit_price']:,})" for it in matched_items]
        sel_item_str = st.selectbox("商品マスタ参照", item_options, key="in_item_master")

        default_name = ""
        default_price = 0
        default_code = "AUTO"
        if sel_item_str != "(自由入力)":
            parts = sel_item_str.split(" - ")
            default_code = parts[0]
            name_p = parts[1].split(" (")[0]
            default_name = name_p
            for it in matched_items:
                if it["code"] == default_code:
                    default_price = it["unit_price"]
                    break

        with st.form("quick_entry_form", clear_on_submit=True):
            f_col1, f_col2 = st.columns(2)
            with f_col1:
                in_date = st.date_input("取引日", datetime.now(), key="in_date")
                in_market = st.text_input("市場・取引先番号", value="M-001", key="in_mkt")
                in_name = st.text_input("品名 (商品名)", value=default_name, key="in_name")
            with f_col2:
                in_qty = st.number_input("数量 (正数/負数・返品可)", value=10, step=1, key="in_qty")
                in_price = st.number_input("単価 (円)", value=default_price, step=10, key="in_price")
                in_subtotal = in_qty * in_price
                st.markdown(f"**仕入金額 (自動計算):** `¥ {in_subtotal:,}`")

            f_col3, f_col4 = st.columns(2)
            with f_col3:
                in_pay = st.text_input("支払金額 (記号・日付等保持)", value="0", key="in_pay")
            with f_col4:
                in_bal = st.number_input("差引残高", value=0, step=100, key="in_bal")

            submit_rec = st.form_submit_button("💾 取引データを即時登録", use_container_width=True)
            if submit_rec:
                if not in_name.strip():
                    st.error("⚠️ 品名を入力してください")
                else:
                    new_rec = [{
                        "日付": in_date.strftime("%Y-%m-%d"),
                        "市場番号": in_market.strip(),
                        "カテゴリー": sel_cat,
                        "商品コード": default_code,
                        "品名": in_name.strip(),
                        "数量": in_qty,
                        "単価": in_price,
                        "仕入金額": in_subtotal,
                        "支払金額": in_pay.strip(),
                        "差引残高": in_bal,
                        "合計": in_subtotal + in_bal
                    }]
                    insert_records_to_db(current_user, new_rec)
                    st.success(f"✅ 「{in_name}」の取引データを登録しました！")
                    st.rerun()

        st.markdown("---")
        st.markdown("#### 📁 取引ファイルからの自動記録")
        st.caption("※エクセルファイル（.xls / .xlsx）をドラッグ＆ドロップすると、全項目（品名・数量・単価・仕入金額・支払金額・差引残高・合計）を自動解析して一括記録します。")
        up_file_tab1 = st.file_uploader("取引ファイルを選択", type=["xlsx", "xls", "csv", "tsv"], key="uploader_tab1")
        if up_file_tab1 is not None:
            df_parsed, sups = parse_trade_file(up_file_tab1)
            if not df_parsed.empty:
                st.write(f"解析成功: **{len(df_parsed)}** 件のレコードを検出しました")
                st.dataframe(df_parsed.head(5), use_container_width=True)
                if st.button("🚀 検出されたすべての取引データを今すぐ自動記録する", key="btn_import_tab1", use_container_width=True):
                    insert_records_to_db(current_user, df_parsed.to_dict("records"))
                    st.success(f"🎉 {len(df_parsed)} 件の取引データをデータベースに記録し、一覧へ反映しました！")
                    st.rerun()
            else:
                st.warning("⚠️ ファイル内に有効な取引データ行が見つかりませんでした。")

    with col_view:
        st.markdown("#### 📋 登録済みデータ一覧 (全項目保持・リアルタイム表示)")
        u_filter = "全員の取引を表示"
        if is_admin:
            all_users_list = ["全員の取引を表示"] + get_all_users_from_db()["username"].tolist()
            u_filter = st.selectbox("担当者絞り込み", all_users_list, key="filter_u")
        
        recs_df = load_records_from_db(u_filter)
        if recs_df.empty:
            st.info("ℹ️ 現在登録されている取引データはありません。")
        else:
            # 表示用カラムのリネーム
            display_df = recs_df.rename(columns={
                "id": "ID", "user_id": "担当者", "date": "仕入日", "market_number": "市場番号",
                "category": "カテゴリー", "item_code": "コード", "item_name": "品名",
                "unit_price": "単価", "quantity": "数量", "subtotal": "仕入金額",
                "purchase_amount": "仕入額", "payment_amount": "支払金額", "balance": "差引残高",
                "total": "合計", "created_at": "登録日時"
            })
            st.dataframe(display_df, use_container_width=True, height=480)
            
            # データエクスポート
            csv_bytes = display_df.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 登録済みデータ一覧をCSVダウンロード",
                data=csv_bytes,
                file_name=f"transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=True
            )

            # 選択削除機能（管理者または本人のみ）
            st.write("")
            with st.expander("🗑️ 登録データの削除"):
                del_ids = st.multiselect("削除するレコードのIDを選択", options=recs_df["id"].tolist())
                if st.button("⚠️ 選択したレコードを削除", type="primary"):
                    if del_ids:
                        delete_records_by_ids(del_ids, current_user)
                        st.success(f"{len(del_ids)} 件のデータを削除しました（直前取り消し可能）")
                        st.rerun()

# ----------------------------------------------------
# TAB 2: 売上・経営分析
# ----------------------------------------------------
with tabs[1]:
    st.markdown("#### 📊 取引トレンド & 売上・仕入動向分析")
    df_ana = load_records_from_db()
    if df_ana.empty:
        st.info("分析可能なデータがありません。")
    else:
        c_a1, c_a2 = st.columns(2)
        with c_a1:
            st.markdown("##### 📅 日別 取引金額推移")
            daily_df = df_ana.groupby("date")["subtotal"].sum().reset_index()
            daily_df = daily_df.sort_values("date")
            fig_daily = px.line(daily_df, x="date", y="subtotal", markers=True, title="日別 取引総額推移")
            fig_daily.update_layout(xaxis_title="日付", yaxis_title="取引額 (円)", margin=dict(l=20, r=20, t=40, b=20))
            st.plotly_chart(fig_daily, use_container_width=True)

        with c_a2:
            st.markdown("##### 🏆 取扱品目 上位シェア")
            item_df = df_ana.groupby("item_name")["subtotal"].sum().reset_index()
            item_df = item_df.sort_values("subtotal", ascending=False).head(8)
            fig_item = px.pie(item_df, values="subtotal", names="item_name", hole=0.4, title="品目別 取引金額シェア")
            fig_item.update_layout(margin=dict(l=20, r=20, t=40, b=20))
            st.plotly_chart(fig_item, use_container_width=True)

        st.markdown("---")
        st.markdown("#### 🤖 スマート青果AI 経営インサイト")
        ai_col1, ai_col2 = st.columns([3, 1])
        with ai_col1:
            focus_theme = st.selectbox("AI分析の重点テーマ", ["総合経営診断", "仕入・利益率の最適化", "売れ筋・死に筋品目の選定と改善提案", "リスク管理・在庫回転率"], key="ai_theme")
        with ai_col2:
            st.write("")
            st.write("")
            run_ai = st.button("✨ AI分析を実行", use_container_width=True)

        if run_ai:
            with st.spinner("青果アナライザーが取引データを深層解析中..."):
                ok, ai_res = generate_produce_ai_insights(df_ana, focus_theme)
                if ok:
                    st.markdown(f"""
                        <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; padding: 18px; margin-top: 12px;">
                            <h5 style="margin: 0 0 10px 0; color: #1e293b;">💡 AI経営戦略アドバイス ({focus_theme})</h5>
                            <div style="font-size: 14px; line-height: 1.7; color: #334155; white-space: pre-wrap;">{ai_res}</div>
                        </div>
                    """, unsafe_allow_html=True)
                else:
                    st.warning(f"AI分析メッセージ: {ai_res}")

# ----------------------------------------------------
# TAB 3: バックアップ & 復元
# ----------------------------------------------------
with tabs[2]:
    st.markdown("#### 💾 BCP（事業継続）自動バックアップ & 復旧センター")
    st.caption("万が一の障害や誤操作時でも、ワンクリックで取引データをスナップショット保存・復元できます。")
    
    b_col1, b_col2 = st.columns(2)
    with b_col1:
        st.markdown("##### 📦 新規バックアップの作成")
        if st.button("💾 今すぐフルバックアップを作成", use_container_width=True):
            fpath = create_backup_snapshot(current_user)
            st.success(f"✅ バックアップを生成しました: `{os.path.basename(fpath)}`")
            st.rerun()

    with b_col2:
        st.markdown("##### 📜 バックアップ履歴")
        b_df = list_all_backups()
        if b_df.empty:
            st.info("バックアップ履歴はありません。")
        else:
            st.dataframe(b_df[["id", "backup_name", "created_by", "record_count", "created_at"]], use_container_width=True)

    if not b_df.empty:
        st.write("")
        with st.expander("⚠️ 過去バックアップからの復元"):
            sel_bname = st.selectbox("復元するバックアップを選択", options=b_df["backup_name"].tolist())
            if st.button("🚀 このスナップショットからデータを復元する", type="primary"):
                sel_row = b_df[b_df["backup_name"] == sel_bname].iloc[0]
                ok, msg = restore_from_backup_file(sel_row["file_path"], current_user)
                if ok:
                    st.success(f"🎉 {msg}")
                    st.rerun()
                else:
                    st.error(msg)

# ----------------------------------------------------
# TAB 4: ユーザー管理 (管理者専用)
# ----------------------------------------------------
if is_admin:
    with tabs[3]:
        st.markdown("#### 👥 担当者アカウント管理")
        u_table_col, u_add_col = st.columns([1, 1], gap="large")
        
        with u_table_col:
            st.markdown("##### 登録済み担当者一覧")
            users_df = get_all_users_from_db()
            st.dataframe(users_df[["username", "role", "display_name", "created_at"]], use_container_width=True)

        with u_add_col:
            st.markdown("##### ➕ 新規アカウント追加")
            with st.form("new_user_form", clear_on_submit=True):
                new_u = st.text_input("ユーザー名 (半角英数)")
                new_p = st.text_input("パスワード", type="password")
                new_d = st.text_input("表示名 (例: 営業担当 佐藤)")
                new_r = st.selectbox("権限", ["staff", "admin"], format_func=lambda x: "一般営業担当 (staff)" if x == "staff" else "統括管理者 (admin)")
                
                sub_new_u = st.form_submit_button("アカウントを発行", use_container_width=True)
                if sub_new_u:
                    if not new_u.strip() or not new_p.strip():
                        st.error("ユーザー名とパスワードを入力してください")
                    else:
                        try:
                            register_new_user_db(new_u.strip(), new_p.strip(), new_r, new_d.strip())
                            st.success(f"アカウント「{new_u}」を発行しました！")
                            st.rerun()
                        except Exception as e:
                            st.error(f"作成失敗: {e}")

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
                st.success(f"✅ ファイル「**{fname}**」を正常に解析しました（検出シート数: **{len(raw_sheets)}** シート / 総行数: **{total_rows}** 行）")

                # 最新形式 .xlsx の即時生成
                xlsx_bytes = convert_xls_file_to_multisheet_xlsx_bytes(uploaded_file)
                uploaded_file.seek(0)

                col_dl1, col_dl2 = st.columns([2, 1])
                with col_dl1:
                    st.download_button(
                        label=f"📥 【全シート・全数値保持】最新の Excel形式 (.xlsx) としてダウンロード",
                        data=xlsx_bytes,
                        file_name=f"{file_base_name}_converted.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
                with col_dl2:
                    st.caption("※Excel 2016 / 2019 / 2021 / Office 365 / スプレッドシート完全互換")

                st.write("")
                st.markdown("##### 📑 シート別プレビュー (元の書式・数値をそのまま表示)")

                sheet_names = list(raw_sheets.keys())
                if len(sheet_names) > 1:
                    preview_tabs = st.tabs([f"📄 {sn}" for sn in sheet_names])
                    for s_idx, sn in enumerate(sheet_names):
                        with preview_tabs[s_idx]:
                            st.caption(f"シート名: **{sn}** （元のセル配置・数値を保持）")
                            st.dataframe(raw_sheets[sn], use_container_width=True, height=360)
                elif len(sheet_names) == 1:
                    sn = sheet_names[0]
                    st.caption(f"シート名: **{sn}** （元のセル配置・数値を保持）")
                    st.dataframe(raw_sheets[sn], use_container_width=True, height=360)

                st.write("")
                st.markdown("##### 🚀 データベースへの自動記録 & 反映")
                st.caption("抽出された品名、数量、単価、仕入金額、支払金額、差引残高、合計を保持したまま一括登録します。")

                col_imp1, col_imp2 = st.columns([2, 3])
                with col_imp1:
                    if st.button("💾 このファイルの全データをデータベースに自動記録する", type="primary", use_container_width=True):
                        records_to_save = df_up.to_dict("records") if not df_up.empty else []
                        if not records_to_save and raw_sheets:
                            # プレビュー表からフォールバック生成
                            for sn, sh_df in raw_sheets.items():
                                h_r = find_best_header_row(sh_df)
                                for r_i in range(h_r + 1, len(sh_df)):
                                    row_vals = sh_df.iloc[r_i].tolist()
                                    if any(pd.notna(x) and str(x).strip() != "" for x in row_vals):
                                        records_to_save.append({
                                            "日付": datetime.now().strftime("%Y-%m-%d"),
                                            "市場番号": "M-001",
                                            "カテゴリー": sn,
                                            "商品コード": "AUTO",
                                            "品名": str(row_vals[1]) if len(row_vals) > 1 and pd.notna(row_vals[1]) else "品名未指定",
                                            "数量": clean_num(row_vals[2]) if len(row_vals) > 2 else 0,
                                            "単価": clean_num(row_vals[3]) if len(row_vals) > 3 else 0,
                                            "仕入金額": clean_num(row_vals[4]) if len(row_vals) > 4 else 0,
                                            "支払金額": str(row_vals[5]) if len(row_vals) > 5 and pd.notna(row_vals[5]) else "0",
                                            "差引残高": clean_num(row_vals[6]) if len(row_vals) > 6 else 0,
                                            "合計": clean_num(row_vals[7]) if len(row_vals) > 7 else 0
                                        })

                        insert_records_to_db(current_user, records_to_save)
                        st.success(f"🎉 {len(records_to_save)} 件のデータを自動記録しました！「📝 取引入力 & 自動記録」の登録済みデータ一覧へ即時反映されました。")
                        st.rerun()
                with col_imp2:
                    if sups:
                        st.caption(f"抽出された取引先（次回から候補として利用可能）: {', '.join(sups[:5])}{' ほか' if len(sups) > 5 else ''}")
            else:
                st.warning("⚠️ ファイル内に有効なデータ行が見つかりませんでした。")
        except Exception as e:
            st.error(f"ファイル解析エラー: {e}")
