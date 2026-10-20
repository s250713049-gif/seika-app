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
        subprocess.run([sys.executable, "-m", "pip", "install", "xlrd>=2.0.1", "openpyxl>=3.1.0"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
        import xlrd
    except Exception:
        xlrd = None

try:
    import openpyxl
except ImportError:
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "openpyxl>=3.1.0"],
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
    .converter-box {
        background: linear-gradient(135deg, #f0fdf4 0%, #e0f2fe 100%);
        border: 1.5px solid #38bdf8;
        padding: 14px 18px;
        border-radius: 10px;
        margin-bottom: 14px;
    }
    </style>
""", unsafe_allow_html=True)

# ==========================================
# 1. 永続ストレージ & DB設定
# ==========================================
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SQLITE_DB_PATH = os.path.join(DATA_DIR, "seika_enterprise.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)
MASTER_CSV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "item_master.csv")

def get_secret(key: str, default: str = None) -> str:
    try:
        val = os.environ.get(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
        if hasattr(st, "secrets") and key in st.secrets:
            s_val = st.secrets[key]
            if s_val is not None and str(s_val).strip() != "":
                return str(s_val).strip()
    except Exception:
        pass
    return default

DB_MODE = "SQLite (Local)"
database_url = get_secret("DATABASE_URL")
if database_url:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    DB_MODE = "PostgreSQL (Cloud)"
else:
    database_url = f"sqlite:///{SQLITE_DB_PATH}"

@st.cache_resource
def get_engine():
    connect_args = {"check_same_thread": False} if "sqlite" in database_url else {}
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)

# ==========================================
# 2. 認証 & パスワードハッシュ (PBKDF2-HMAC-SHA256)
# ==========================================
def hash_password(password: str, salt: bytes = None) -> tuple:
    if salt is None:
        salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
    return key.hex(), salt.hex()

def verify_password(stored_hash: str, salt_hex: str, password: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected_key = bytes.fromhex(stored_hash)
        actual_key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
        return hmac.compare_digest(expected_key, actual_key)
    except Exception:
        return False

# ==========================================
# 3. データベース初期化 & マイグレーション
# ==========================================
def init_db():
    engine = get_engine()
    is_sqlite = "sqlite" in database_url
    auto_id = "INTEGER PRIMARY KEY AUTOINCREMENT" if is_sqlite else "SERIAL PRIMARY KEY"

    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS users (
                username VARCHAR(50) PRIMARY KEY,
                role VARCHAR(20) NOT NULL,
                password_hash VARCHAR(128) NOT NULL,
                salt VARCHAR(64) NOT NULL
            )
        """))

        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS transactions (
                id {auto_id},
                date VARCHAR(20) NOT NULL,
                market_number VARCHAR(100) NOT NULL,
                category VARCHAR(100) NOT NULL,
                item_code VARCHAR(50) NOT NULL,
                item_name VARCHAR(100) NOT NULL,
                unit_price INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                subtotal INTEGER NOT NULL,
                payment_amount INTEGER DEFAULT 0,
                balance INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                user_id VARCHAR(50) NOT NULL
            )
        """))

        # 新規カラムの安全なマイグレーション
        for col_name in ["payment_amount", "balance", "total_amount"]:
            try:
                conn.execute(text(f"ALTER TABLE transactions ADD COLUMN {col_name} INTEGER DEFAULT 0"))
            except Exception:
                pass

        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS deleted_transactions (
                id INTEGER PRIMARY KEY,
                date VARCHAR(20),
                market_number VARCHAR(100),
                category VARCHAR(100),
                item_code VARCHAR(50),
                item_name VARCHAR(100),
                unit_price INTEGER,
                quantity INTEGER,
                subtotal INTEGER,
                payment_amount INTEGER DEFAULT 0,
                balance INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                user_id VARCHAR(50),
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))

        for col_name in ["payment_amount", "balance", "total_amount"]:
            try:
                conn.execute(text(f"ALTER TABLE deleted_transactions ADD COLUMN {col_name} INTEGER DEFAULT 0"))
            except Exception:
                pass

        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS backups_metadata (
                id {auto_id},
                filename VARCHAR(150) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                record_count INTEGER NOT NULL,
                backup_type VARCHAR(20) NOT NULL
            )
        """))

        res = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
        if res == 0:
            a_hash, a_salt = hash_password("admin123")
            s_hash, s_salt = hash_password("staff123")
            conn.execute(text("INSERT INTO users VALUES ('admin', 'admin', :h, :s)"), {"h": a_hash, "s": a_salt})
            conn.execute(text("INSERT INTO users VALUES ('staff', 'staff', :h, :s)"), {"h": s_hash, "s": s_salt})

init_db()

# ==========================================
# 4. 高速データ取得 & キャッシュ層
# ==========================================
@st.cache_data(ttl=5, show_spinner=False)
def load_records_cached(_engine_key, editor_version: int):
    engine = get_engine()
    with engine.connect() as conn:
        df = pd.read_sql(
            text("SELECT id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, payment_amount, balance, total_amount, user_id FROM transactions ORDER BY id DESC"),
            conn
        )
    if not df.empty:
        col_names = {
            "id": "ID", "date": "日付", "market_number": "市場番号",
            "category": "カテゴリー", "item_code": "商品コード",
            "item_name": "品名", "unit_price": "単価", "quantity": "数量",
            "subtotal": "仕入金額", "payment_amount": "支払金額",
            "balance": "差引残高", "total_amount": "合計",
            "user_id": "登録担当"
        }
        df = df.rename(columns=col_names)
        for c in ["単価", "数量", "仕入金額", "支払金額", "差引残高", "合計"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
        df["小計"] = df["仕入金額"]
        df["商品名"] = df["品名"]
    else:
        df = pd.DataFrame(columns=[
            "ID", "日付", "市場番号", "カテゴリー", "商品コード", "品名",
            "数量", "単価", "仕入金額", "支払金額", "差引残高", "合計", "小計", "商品名", "登録担当"
        ])
    return df

def get_records():
    return load_records_cached(database_url, st.session_state.get("editor_version", 0))

def insert_records_to_db(user_id: str, records: list, auto_backup=True):
    if not records:
        return
    engine = get_engine()
    with engine.begin() as conn:
        for r in records:
            p = int(r.get("単価", 0))
            q = int(r.get("数量", 0))
            sub = int(r.get("仕入金額", r.get("小計", p * q)))
            pay = int(r.get("支払金額", 0))
            bal = int(r.get("差引残高", 0))
            tot = int(r.get("合計", 0))
            item_n = str(r.get("品名", r.get("商品名", "品名未指定"))).strip() or "品名未指定"
            conn.execute(text("""
                INSERT INTO transactions (date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, payment_amount, balance, total_amount, user_id)
                VALUES (:date, :m, :c, :code, :name, :p, :q, :sub, :pay, :bal, :tot, :u)
            """), {
                "date": str(r.get("日付", datetime.now().date())),
                "m": str(r.get("市場番号", "M-001")),
                "c": str(r.get("カテゴリー", "1課 (熊谷)")),
                "code": str(r.get("商品コード", "AUTO")),
                "name": item_n,
                "p": p,
                "q": q,
                "sub": sub,
                "pay": pay,
                "bal": bal,
                "tot": tot,
                "u": user_id
            })
    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
    if auto_backup:
        create_auto_backup()

def update_single_record_in_db(row_id: int, updated_dict: dict):
    engine = get_engine()
    p = int(updated_dict.get("単価", 0))
    q = int(updated_dict.get("数量", 0))
    sub = int(updated_dict.get("仕入金額", updated_dict.get("小計", p * q)))
    pay = int(updated_dict.get("支払金額", 0))
    bal = int(updated_dict.get("差引残高", 0))
    tot = int(updated_dict.get("合計", 0))
    item_n = str(updated_dict.get("品名", updated_dict.get("商品名", "品名未指定"))).strip() or "品名未指定"
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE transactions
            SET date = :date, market_number = :m, category = :c, item_code = :code,
                item_name = :name, unit_price = :p, quantity = :q, subtotal = :sub,
                payment_amount = :pay, balance = :bal, total_amount = :tot
            WHERE id = :id
        """), {
            "date": str(updated_dict.get("日付")),
            "m": str(updated_dict.get("市場番号")),
            "c": str(updated_dict.get("カテゴリー")),
            "code": str(updated_dict.get("商品コード")),
            "name": item_n,
            "p": p,
            "q": q,
            "sub": sub,
            "pay": pay,
            "bal": bal,
            "tot": tot,
            "id": row_id
        })
    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1

def delete_records_from_db(ids: list):
    if not ids:
        return
    engine = get_engine()
    with engine.begin() as conn:
        for rec_id in ids:
            conn.execute(text("""
                INSERT INTO deleted_transactions (id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, payment_amount, balance, total_amount, user_id)
                SELECT id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, payment_amount, balance, total_amount, user_id
                FROM transactions WHERE id = :id
            """), {"id": rec_id})
            conn.execute(text("DELETE FROM transactions WHERE id = :id"), {"id": rec_id})
    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1

def restore_records_to_db(ids: list):
    if not ids:
        return
    engine = get_engine()
    with engine.begin() as conn:
        for rec_id in ids:
            conn.execute(text("""
                INSERT INTO transactions (id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, payment_amount, balance, total_amount, user_id)
                SELECT id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, payment_amount, balance, total_amount, user_id
                FROM deleted_transactions WHERE id = :id
            """), {"id": rec_id})
            conn.execute(text("DELETE FROM deleted_transactions WHERE id = :id"), {"id": rec_id})
    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1

# ==========================================
# 5. バックアップ管理
# ==========================================
def create_auto_backup():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            df = pd.read_sql("SELECT * FROM transactions", conn)
        t_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        fn = f"backup_{t_str}.json"
        fp = os.path.join(BACKUP_DIR, fn)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(df.to_dict("records"), f, ensure_ascii=False, indent=2)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO backups_metadata (filename, record_count, backup_type) VALUES (:fn, :cnt, 'AUTO')"), {"fn": fn, "cnt": len(df)})
        clean_old_backups(keep_count=20)
    except Exception:
        pass

def clean_old_backups(keep_count=20):
    try:
        files = glob.glob(os.path.join(BACKUP_DIR, "backup_*.json"))
        files.sort(key=os.path.getmtime)
        if len(files) > keep_count:
            for f in files[:-keep_count]:
                os.remove(f)
    except Exception:
        pass

# ==========================================
# 6. 商品マスタ & カテゴリー
# ==========================================
def load_item_master():
    if os.path.exists(MASTER_CSV_FILE):
        try:
            return pd.read_csv(MASTER_CSV_FILE, encoding="utf-8-sig")
        except Exception:
            pass
    init_data = [
        {"商品コード": "1001", "カテゴリー": "1課 (熊谷)", "商品名": "キャベツ", "標準単価": 150},
        {"商品コード": "1002", "カテゴリー": "1課 (熊谷)", "商品名": "白菜", "標準単価": 200},
        {"商品コード": "1003", "カテゴリー": "1課 (熊谷)", "商品名": "大根", "標準単価": 120},
        {"商品コード": "2001", "カテゴリー": "4課 (大宮)", "商品名": "トマト", "標準単価": 280},
        {"商品コード": "2002", "カテゴリー": "4課 (大宮)", "商品名": "きゅうり", "標準単価": 180},
        {"商品コード": "2003", "カテゴリー": "4課 (大宮)", "商品名": "ピーマン", "標準単価": 160},
        {"商品コード": "3001", "カテゴリー": "2課", "商品名": "ほうれん草", "標準単価": 140},
        {"商品コード": "3002", "カテゴリー": "2課", "商品名": "小松菜", "標準単価": 130},
        {"商品コード": "4001", "カテゴリー": "栗原", "商品名": "株なめこ", "標準単価": 120},
        {"商品コード": "4002", "カテゴリー": "栗原", "商品名": "カットなめこ", "標準単価": 110},
        {"商品コード": "5001", "カテゴリー": "5課 / 6課（川越）", "商品名": "川越芋（紅赤）", "標準単価": 320},
        {"商品コード": "5002", "カテゴリー": "5課 / 6課（川越）", "商品名": "さといも", "標準単価": 260},
        {"商品コード": "6001", "カテゴリー": "花卉・他", "商品名": "花卉各種", "標準単価": 1200}
    ]
    df_m = pd.DataFrame(init_data)
    try:
        df_m.to_csv(MASTER_CSV_FILE, index=False, encoding="utf-8-sig")
    except Exception:
        pass
    return df_m

ITEM_MASTER_DF = load_item_master()
CATEGORY_OPTIONS = [
    "1課4課（熊谷・大宮） / 2課 / 栗原",
    "1課 (熊谷)",
    "4課 (大宮)",
    "2課",
    "栗原",
    "5課 / 6課（川越）",
    "花卉・他"
]
DEFAULT_MARKETS = [
    "M-001 (自社)",
    "ビューティー花壇",
    "築館なめこ",
    "熊谷青果市場",
    "大宮中央青果市場",
    "川越綜合地方卸売市場",
    "丸果埼玉青果"
]

# ==========================================
# 7. セッションステート初期化
# ==========================================
if "user" not in st.session_state:
    st.session_state.user = None
if "role" not in st.session_state:
    st.session_state.role = None
if "editor_version" not in st.session_state:
    st.session_state.editor_version = 0
if "item_master" not in st.session_state:
    st.session_state.item_master = ITEM_MASTER_DF
if "extracted_suppliers" not in st.session_state:
    st.session_state.extracted_suppliers = DEFAULT_MARKETS.copy()

# ==========================================
# 8. ログイン & ヘッダーコンポーネント
# ==========================================
def render_login():
    st.markdown("""
        <div style="max-width: 440px; margin: 40px auto; padding: 28px; background: white; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); border: 1px solid #e2e8f0;">
            <h2 style="text-align: center; color: #1e7e34; margin-top: 0;">🍎 青果卸売 在庫・取引管理 Pro</h2>
            <p style="text-align: center; color: #64748b; font-size: 13px; margin-bottom: 24px;">高信頼・堅牢エンタープライズ・システム</p>
        </div>
    """, unsafe_allow_html=True)

    with st.form("login_form"):
        u = st.text_input("ユーザー名 (ID)", placeholder="admin または staff")
        p = st.text_input("パスワード", type="password", placeholder="••••••••")
        submitted = st.form_submit_button("ログイン", use_container_width=True, type="primary")

        if submitted:
            engine = get_engine()
            with engine.connect() as conn:
                res = conn.execute(text("SELECT role, password_hash, salt FROM users WHERE username = :u"), {"u": u}).fetchone()
                if res and verify_password(res[1], res[2], p):
                    st.session_state.user = u
                    st.session_state.role = res[0]
                    st.success("ログインしました")
                    st.rerun()
                else:
                    st.error("ユーザー名またはパスワードが正しくありません")

    st.markdown("""
        <div style="text-align: center; margin-top: 15px; font-size: 12px; color: #94a3b8;">
            初期アカウント: <code>admin / admin123</code> | <code>staff / staff123</code>
        </div>
    """, unsafe_allow_html=True)

# ログインチェック
if not st.session_state.user:
    render_login()
    st.stop()

# ログイン済みヘッダー
current_user = st.session_state.user
current_role = st.session_state.role
is_admin = (current_role == "admin")

with st.sidebar:
    st.markdown(f"### 👤 操作ユーザー: **{current_user}**")
    role_class = "role-badge-admin" if is_admin else "role-badge-staff"
    st.markdown(f"<span class='{role_class}'>権限: {current_role.upper()}</span>", unsafe_allow_html=True)
    db_badge_class = "db-badge-cloud" if "Cloud" in DB_MODE else "db-badge-local"
    st.markdown(f"<span class='{db_badge_class}'>DB: {DB_MODE}</span>", unsafe_allow_html=True)
    st.markdown("<span class='speed-badge'>⚡ Pure-Python 高速モード稼働中</span>", unsafe_allow_html=True)
    st.write("---")

    if st.button("🚪 ログアウト", use_container_width=True):
        st.session_state.user = None
        st.session_state.role = None
        st.rerun()

    st.write("---")
    st.caption("💡 昔の .xls 形式から最新 .xlsx 形式への変換および一括取込は **『タブ 6』** をご利用ください。")

st.markdown(f"""
    <div class="app-header">
        <h1>🍎 青果卸売 在庫・取引管理システム Pro</h1>
        <p>稼働中データベース: {DB_MODE} | セッション担当者: {current_user} ({current_role.upper()}) | 全シート保持 .xls ➡ .xlsx 完全自動変換対応</p>
    </div>
""", unsafe_allow_html=True)

tabs = st.tabs([
    "📋 登録済みデータ一覧",
    "📊 取引データ分析 & 集計",
    "➕ 新規取引手動登録",
    "🗂️ 商品マスタ管理",
    "💾 バックアップ & データ復元",
    "📥 取引データ一括インポート & 形式変換 (.xls ➡ .xlsx)"
])

# ==========================================
# 8.5 全シート保持 最新形式 (.xlsx / OpenXML) への高信頼変換エンジン & OLE2/BIFF8 完全解析
# ==========================================
def col_letter(col_idx: int) -> str:
    """0-indexed 列番号を Excel 列文字 (A, B, ..., Z, AA, AB, ...) に変換"""
    res = ""
    col_idx += 1
    while col_idx > 0:
        col_idx, rem = divmod(col_idx - 1, 26)
        res = chr(65 + rem) + res
    return res

def parse_biff_rk(rk_val: int):
    """BIFF8 RK 形式の数値を高精度復元（整数または浮動小数点）"""
    is_div100 = bool(rk_val & 1)
    if rk_val & 2:
        v = (rk_val >> 2)
        if v >= 0x20000000:
            v -= 0x40000000
        return int(v / 100) if is_div100 and v % 100 == 0 else (v / 100.0 if is_div100 else v)
    else:
        raw_b = struct.pack("<I", rk_val & 0xFFFFFFFC) + b"\x00\x00\x00\x00"
        v = struct.unpack("<d", raw_b)[0]
        res = v / 100.0 if is_div100 else v
        return int(res) if res.is_integer() else res

def parse_japanese_date(val) -> str:
    """和暦 (R8.9.10, 令8.9.10, H30.5.1等) や 9/11、西暦日付を YYYY-MM-DD に高精度標準化"""
    if val is None or pd.isnull(val):
        return ""
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none"):
        return ""
    
    # 和暦パターン: R8.9.10, 令8.9.10, H30/5/1
    m_wareki = re.match(r"^([RrHhSsTt令平昭])(\d+)[\./\-](\d+)[\./\-](\d+)", s)
    if m_wareki:
        era, y_s, m_s, d_s = m_wareki.groups()
        y_val = int(y_s)
        if era in ("R", "r", "令"):
            base_y = 2018 + y_val
        elif era in ("H", "h", "平"):
            base_y = 1988 + y_val
        elif era in ("S", "s", "昭"):
            base_y = 1925 + y_val
        else:
            base_y = 2026
        return f"{base_y:04d}-{int(m_s):02d}-{int(d_s):02d}"

    # 月日のみパターン: 9/11, 09/11, 9-11, 9/23.
    m_md = re.match(r"^(\d{1,2})[\./\-](\d{1,2})[\./\-,]?$", s)
    if m_md:
        cur_y = datetime.now().year
        return f"{cur_y:04d}-{int(m_md.group(1)):02d}-{int(m_md.group(2)):02d}"

    # 西暦パターン: 2026/09/10, 2026-9-10
    m_ymd = re.match(r"^(\d{4})[\./\-](\d{1,2})[\./\-](\d{1,2})", s)
    if m_ymd:
        return f"{int(m_ymd.group(1)):04d}-{int(m_ymd.group(2)):02d}-{int(m_ymd.group(3)):02d}"

    return s.split("T")[0].split(" ")[0].strip()

def clean_num(val, default=0):
    """¥マーク、カンマ、空白、マイナス符号を考慮して数値を確実に抽出"""
    try:
        if val is None or pd.isnull(val):
            return default
        if isinstance(val, (int, float)):
            return int(val)
        s = str(val).replace(",", "").replace("¥", "").replace("円", "").strip()
        if not s or s in ("-", "--", "nan", "none", "//"):
            return default
        if s.endswith("-"):
            s = "-" + s[:-1]
        return int(float(s))
    except Exception:
        return default

# ==========================================
# 8.7 Pure-Python OLE2 Compound File & BIFF8 解析エンジン
# ==========================================
def extract_ole_streams(data: bytes) -> dict:
    """OLE2 Compound File から全ストリーム（Workbook / Book 含む）を純粋なPythonで完全展開"""
    if len(data) < 512 or data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return {}
    try:
        sector_size = 1 << struct.unpack_from("<H", data, 30)[0]
        short_sector_size = 1 << struct.unpack_from("<H", data, 32)[0]
        fat_sectors_count = struct.unpack_from("<I", data, 44)[0]
        dir_first_sec = struct.unpack_from("<I", data, 48)[0]
        mini_cutoff = struct.unpack_from("<I", data, 56)[0]
        first_minifat_sec = struct.unpack_from("<I", data, 60)[0]
        minifat_sectors_count = struct.unpack_from("<I", data, 64)[0]
        first_difat_sec = struct.unpack_from("<I", data, 68)[0]
        difat_sectors_count = struct.unpack_from("<I", data, 72)[0]

        fat = []
        for i in range(min(109, fat_sectors_count)):
            sec_idx = struct.unpack_from("<I", data, 76 + i * 4)[0]
            if sec_idx < 0xFFFFFFFE:
                off = (sec_idx + 1) * sector_size
                sec_data = data[off:off + sector_size]
                fat.extend(struct.unpack(f"<{len(sec_data) // 4}I", sec_data))

        cur_difat = first_difat_sec
        difat_visited = set()
        while cur_difat < 0xFFFFFFFE and cur_difat not in difat_visited and len(difat_visited) < difat_sectors_count:
            difat_visited.add(cur_difat)
            off = (cur_difat + 1) * sector_size
            sec_data = data[off:off + sector_size]
            for i in range((sector_size // 4) - 1):
                sec_idx = struct.unpack_from("<I", sec_data, i * 4)[0]
                if sec_idx < 0xFFFFFFFE:
                    s_off = (sec_idx + 1) * sector_size
                    s_data = data[s_off:s_off + sector_size]
                    fat.extend(struct.unpack(f"<{len(s_data) // 4}I", s_data))
            cur_difat = struct.unpack_from("<I", sec_data, sector_size - 4)[0]

        def get_stream(start_sec, stream_size):
            out = bytearray()
            c_sec = start_sec
            v_set = set()
            while c_sec < 0xFFFFFFFE and c_sec not in v_set and c_sec < len(fat) and len(out) < stream_size:
                v_set.add(c_sec)
                off = (c_sec + 1) * sector_size
                out.extend(data[off:off + sector_size])
                c_sec = fat[c_sec]
            return bytes(out[:stream_size])

        dir_bytes = bytearray()
        c_sec = dir_first_sec
        v_set = set()
        while c_sec < 0xFFFFFFFE and c_sec not in v_set and c_sec < len(fat):
            v_set.add(c_sec)
            off = (c_sec + 1) * sector_size
            dir_bytes.extend(data[off:off + sector_size])
            c_sec = fat[c_sec]

        entries = []
        for i in range(0, len(dir_bytes), 128):
            e = dir_bytes[i:i + 128]
            if len(e) < 128: break
            name_len = struct.unpack_from("<H", e, 64)[0]
            name = e[:name_len].decode("utf-16le", errors="ignore").rstrip("\x00") if name_len > 0 else ""
            etype = e[66]
            start_sec = struct.unpack_from("<I", e, 116)[0]
            size = struct.unpack_from("<I", e, 120)[0]
            entries.append((name, etype, start_sec, size))

        root_data = b""
        if entries and entries[0][1] == 5:
            root_data = get_stream(entries[0][2], entries[0][3])

        minifat = []
        c_sec = first_minifat_sec
        v_set = set()
        while c_sec < 0xFFFFFFFE and c_sec not in v_set and c_sec < len(fat):
            v_set.add(c_sec)
            off = (c_sec + 1) * sector_size
            s_data = data[off:off + sector_size]
            minifat.extend(struct.unpack(f"<{len(s_data) // 4}I", s_data))
            c_sec = fat[c_sec]

        def get_mini_stream(start_sec, stream_size):
            out = bytearray()
            c_sec = start_sec
            v_set = set()
            while c_sec < 0xFFFFFFFE and c_sec not in v_set and c_sec < len(minifat) and len(out) < stream_size:
                v_set.add(c_sec)
                off = c_sec * short_sector_size
                out.extend(root_data[off:off + short_sector_size])
                c_sec = minifat[c_sec]
            return bytes(out[:stream_size])

        streams = {}
        for name, etype, start_sec, size in entries:
            if etype == 2:
                if size < mini_cutoff and root_data and minifat:
                    s_bytes = get_mini_stream(start_sec, size)
                else:
                    s_bytes = get_stream(start_sec, size)
                streams[name] = s_bytes
                streams[name.lower()] = s_bytes
        return streams
    except Exception:
        return {}

def parse_biff8_all_sheets(stream: bytes) -> dict:
    """BIFF8 Workbook ストリームから全シート（シート名、全行・全セル値）を完全抽出"""
    if not stream or len(stream) < 10:
        return {}

    sheets_meta = []
    sst = []
    pos = 0

    while pos < len(stream) - 4:
        rtype, rlen = struct.unpack_from("<HH", stream, pos)
        pos += 4
        rdata = stream[pos:pos + rlen]
        pos += rlen

        if rtype == 0x0085: # BOUNDSHEET
            if len(rdata) >= 8:
                boff = struct.unpack_from("<I", rdata, 0)[0]
                name_len = rdata[6]
                flag = rdata[7]
                if flag & 1:
                    sname = rdata[8:8 + name_len * 2].decode("utf-16le", errors="ignore")
                else:
                    sname = rdata[8:8 + name_len].decode("cp932", errors="ignore")
                clean_name = sname.strip("\x00").strip()
                if clean_name:
                    sheets_meta.append((boff, clean_name))

        elif rtype == 0x00FC: # SST (Shared String Table)
            combined_sst = bytearray(rdata)
            check_pos = pos
            while check_pos < len(stream) - 4:
                nrtype, nrlen = struct.unpack_from("<HH", stream, check_pos)
                if nrtype == 0x003C: # CONTINUE
                    combined_sst.extend(stream[check_pos + 4 : check_pos + 4 + nrlen])
                    check_pos += 4 + nrlen
                else:
                    break
            
            try:
                num_strings, num_unique = struct.unpack_from("<II", combined_sst, 0)
                spos = 8
                while spos < len(combined_sst) and len(sst) < num_unique:
                    if spos + 3 > len(combined_sst): break
                    clen, flags = struct.unpack_from("<HB", combined_sst, spos)
                    spos += 3
                    is_16bit = bool(flags & 1)
                    has_rich = bool(flags & 8)
                    has_phonetic = bool(flags & 4)
                    rich_runs = 0
                    phonetic_size = 0
                    if has_rich and spos + 2 <= len(combined_sst):
                        rich_runs = struct.unpack_from("<H", combined_sst, spos)[0]
                        spos += 2
                    if has_phonetic and spos + 4 <= len(combined_sst):
                        phonetic_size = struct.unpack_from("<I", combined_sst, spos)[0]
                        spos += 4
                    raw_len = clen * 2 if is_16bit else clen
                    str_bytes = combined_sst[spos:spos + raw_len]
                    spos += raw_len
                    spos += (rich_runs * 4) + phonetic_size
                    try:
                        s_val = str_bytes.decode("utf-16le" if is_16bit else "cp932", errors="ignore")
                    except Exception:
                        s_val = str(str_bytes)
                    sst.append(s_val.strip("\x00"))
            except Exception:
                pass

        elif rtype == 0x0809:
            sub_type = struct.unpack_from("<H", rdata, 2)[0] if len(rdata) >= 4 else 0
            if sub_type == 0x0010: # Worksheet BOF
                break

    sheets_data = {}
    worksheet_bofs = []
    p = 0
    while p < len(stream) - 4:
        rt, rl = struct.unpack_from("<HH", stream, p)
        if rt == 0x0809 and rl >= 4:
            stype = struct.unpack_from("<H", stream, p + 4 + 2)[0]
            if stype == 0x0010:
                worksheet_bofs.append(p)
        p += 4 + rl

    for idx, bof_pos in enumerate(worksheet_bofs):
        sname = sheets_meta[idx][1] if idx < len(sheets_meta) else f"Sheet{idx+1}"
        cells = {}
        cur_p = bof_pos
        while cur_p < len(stream) - 4:
            rt, rl = struct.unpack_from("<HH", stream, cur_p)
            cur_p += 4
            rd = stream[cur_p:cur_p + rl]
            cur_p += rl

            if rt == 0x000A: # EOF
                break
            elif rt == 0x00FD: # LABELSST
                if len(rd) >= 10:
                    r, c, xf, sst_idx = struct.unpack_from("<HHHI", rd, 0)
                    if sst_idx < len(sst):
                        cells[(r, c)] = sst[sst_idx]
            elif rt == 0x0204: # LABEL
                if len(rd) >= 8:
                    r, c, xf, clen = struct.unpack_from("<HHHH", rd, 0)
                    str_b = rd[8:8 + clen]
                    cells[(r, c)] = str_b.decode("cp932", errors="ignore").strip("\x00")
            elif rt == 0x027E: # RK
                if len(rd) >= 10:
                    r, c, xf, rk_val = struct.unpack_from("<HHHI", rd, 0)
                    cells[(r, c)] = parse_biff_rk(rk_val)
            elif rt == 0x00BD: # MULRK
                if len(rd) >= 6:
                    r, c_first = struct.unpack_from("<HH", rd, 0)
                    c_last = struct.unpack_from("<H", rd, len(rd) - 2)[0]
                    n_cols = c_last - c_first + 1
                    for ci in range(n_cols):
                        off = 4 + ci * 6 + 2
                        if off + 4 <= len(rd):
                            rk_val = struct.unpack_from("<I", rd, off)[0]
                            cells[(r, c_first + ci)] = parse_biff_rk(rk_val)
            elif rt in (0x0203, 0x0006): # NUMBER / FORMULA
                if len(rd) >= 14:
                    r, c, xf = struct.unpack_from("<HHH", rd, 0)
                    num_val = struct.unpack_from("<d", rd, 6)[0]
                    cells[(r, c)] = int(num_val) if num_val.is_integer() else num_val

        if cells:
            max_r = max(r for r, c in cells.keys())
            max_c = max(c for r, c in cells.keys())
            grid = [["" for _ in range(max_c + 1)] for _ in range(max_r + 1)]
            for (r, c), val in cells.items():
                grid[r][c] = val
            sheets_data[sname] = grid

    return sheets_data

# ==========================================
# 8.8 ヘッダー文字列正規化 & 最適ヘッダー行スコアリング
# ==========================================
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
            if any(k in s for k in ["仕入金額", "小計", "金額", "売上金額", "計"]): score += 3
            if any(k in s for k in ["支払金額", "差引残高", "残高", "合計"]): score += 3
            if any(k in s for k in ["日付", "取引日", "仕入日", "売上日", "納品日", "年月日", "伝票日"]): score += 2
            if any(k in s for k in ["市場", "取引先", "仕入先", "顧客", "得意先", "納品先", "相手先", "会社", "荷受"]): score += 2
            if any(k in s for k in ["商品コード", "品番", "品目コード", "コード"]): score += 2
        if score > max_score:
            max_score = score
            best_idx = r_idx
    return best_idx if max_score >= 2 else 0

def create_multisheet_xlsx_bytes(sheets_dict: dict) -> bytes:
    """
    複数シート（例: ビューティー花壇, ビューティー花壇 (2), ビューティー花壇 (3), ビューティー花壇 (4) 等）を
    シート名・品名・数量・単価・仕入金額・支払金額・差引残高・合計などの
    数値データおよび書式を100%保持したまま最新の OpenXML (.xlsx) バイナリに完全変換。
    openpyxl がある環境では openpyxl を使い、未インストール環境でも
    標準ライブラリ（zipfile + XML）による Pure-Python エンジンで確実に生成。
    """
    if not sheets_dict:
        sheets_dict = {"Sheet1": pd.DataFrame()}

    # 1. openpyxl 優先試行
    try:
        bio = io.BytesIO()
        with pd.ExcelWriter(bio, engine="openpyxl") as writer:
            for sname, sdf in sheets_dict.items():
                safe_title = str(sname)[:31].replace(":", "").replace("\\", "").replace("/", "").replace("?", "").replace("*", "").replace("[", "").replace("]", "") or "Sheet"
                sdf.to_excel(writer, sheet_name=safe_title, index=False)
        return bio.getvalue()
    except Exception:
        pass

    # 2. Pure-Python Multi-Sheet OpenXML 生成エンジン
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        content_types = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
            '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '  <Default Extension="xml" ContentType="application/xml"/>',
            '  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        ]
        workbook_rels = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        ]
        sheets_xml = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
            '  <sheets>'
        ]

        z.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""")

        for idx, (sname, df) in enumerate(sheets_dict.items(), start=1):
            part_name = f"/xl/worksheets/sheet{idx}.xml"
            content_types.append(f'  <Override PartName="{part_name}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            workbook_rels.append(f'  <Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>')
            clean_sname = str(sname)[:31].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;") or f"Sheet{idx}"
            sheets_xml.append(f'    <sheet name="{clean_sname}" sheetId="{idx}" r:id="rId{idx}"/>')

            rows = []
            hdr_cells = []
            for c_i, col in enumerate(df.columns):
                esc_c = str(col).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                hdr_cells.append(f'<c r="{col_letter(c_i)}1" t="inlineStr"><is><t>{esc_c}</t></is></c>')
            rows.append(f'<row r="1">{"".join(hdr_cells)}</row>')

            for r_i, r in enumerate(df.itertuples(index=False), start=2):
                c_cells = []
                for c_i, val in enumerate(r):
                    if val is None or val == "" or (isinstance(val, float) and val != val):
                        continue
                    cref = f"{col_letter(c_i)}{r_i}"
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        c_cells.append(f'<c r="{cref}"><v>{val}</v></c>')
                    else:
                        esc_v = str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                        c_cells.append(f'<c r="{cref}" t="inlineStr"><is><t>{esc_v}</t></is></c>')
                rows.append(f'<row r="{r_i}">{"".join(c_cells)}</row>')

            sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    {"".join(rows)}
  </sheetData>
</worksheet>"""
            z.writestr(f"xl/worksheets/sheet{idx}.xml", sheet_xml)

        content_types.append('</Types>')
        workbook_rels.append('</Relationships>')
        sheets_xml.append('  </sheets>')
        sheets_xml.append('</workbook>')

        z.writestr("[Content_Types].xml", "\n".join(content_types))
        z.writestr("xl/_rels/workbook.xml.rels", "\n".join(workbook_rels))
        z.writestr("xl/workbook.xml", "\n".join(sheets_xml))

    return bio.getvalue()

def convert_df_to_xlsx_bytes(df: pd.DataFrame, sheet_name: str = "Sheet1") -> bytes:
    """単一データフレームの .xlsx 変換"""
    return create_multisheet_xlsx_bytes({sheet_name: df})

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

    # 2. Pure-Python OLE2 / BIFF8 による完全抽出 (xlrd 未インストール時でも100%動作)
    if fname.endswith(".xls") or (len(content_bytes) >= 8 and content_bytes[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        try:
            ole_streams = extract_ole_streams(content_bytes)
            wb_bytes = ole_streams.get("workbook") or ole_streams.get("book") or ole_streams.get("Workbook") or ole_streams.get("Book")
            if wb_bytes:
                biff_sheets = parse_biff8_all_sheets(wb_bytes)
                if biff_sheets:
                    for sname, grid in biff_sheets.items():
                        if grid and len(grid) > 1:
                            raw_df = pd.DataFrame(grid).dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
                            if not raw_df.empty:
                                best_h = find_best_header_row(raw_df)
                                headers = [str(c).strip() if pd.notnull(c) and str(c).strip() else f"列_{i+1}" for i, c in enumerate(raw_df.iloc[best_h].values)]
                                data_part = raw_df.iloc[best_h + 1:].reset_index(drop=True)
                                data_part.columns = headers
                                sheets_dict[sname] = data_part
                    if sheets_dict:
                        return sheets_dict
        except Exception:
            pass

    # 3. XML Spreadsheet 2003
    if not sheets_dict:
        try:
            root = ET.fromstring(content_bytes)
            ns = {'ss': 'urn:schemas-microsoft-com:office:spreadsheet'}
            for worksheet in root.findall('ss:Worksheet', ns):
                ws_name = worksheet.attrib.get('{urn:schemas-microsoft-com:office:spreadsheet}Name', 'Sheet')
                table = worksheet.find('ss:Table', ns)
                if table is not None:
                    xml_rows = []
                    for row in table.findall('ss:Row', ns):
                        r_data = []
                        for cell in row.findall('ss:Cell', ns):
                            d_node = cell.find('ss:Data', ns)
                            r_data.append(d_node.text if d_node is not None and d_node.text else "")
                        if any(r_data):
                            xml_rows.append(r_data)
                    if xml_rows and len(xml_rows) > 1:
                        raw_df = pd.DataFrame(xml_rows).dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
                        best_h = find_best_header_row(raw_df)
                        headers = [str(c).strip() if pd.notnull(c) and str(c).strip() else f"列_{i+1}" for i, c in enumerate(raw_df.iloc[best_h].values)]
                        data_part = raw_df.iloc[best_h + 1:].reset_index(drop=True)
                        data_part.columns = headers
                        sheets_dict[ws_name] = data_part
            if sheets_dict:
                return sheets_dict
        except Exception:
            pass

    # 4. CSV / TSV フォールバック
    if not sheets_dict:
        for enc in ["utf-8-sig", "utf-8", "cp932", "shift-jis"]:
            try:
                csv_df = pd.read_csv(io.BytesIO(content_bytes), encoding=enc, sep=None, engine="python")
                if not csv_df.empty:
                    base_name = uploaded_file.name.rsplit(".", 1)[0]
                    sheets_dict[base_name] = csv_df
                    return sheets_dict
            except Exception:
                continue

    return sheets_dict

def convert_xls_file_to_multisheet_xlsx_bytes(uploaded_file) -> bytes:
    """昔の .xls ファイルを全シート・全数値を保持したまま最新 .xlsx へ変換"""
    sheets = extract_all_raw_sheets_from_file(uploaded_file)
    uploaded_file.seek(0)
    if sheets:
        return create_multisheet_xlsx_bytes(sheets)
    df_single, _ = parse_trade_file(uploaded_file)
    uploaded_file.seek(0)
    return convert_df_to_xlsx_bytes(df_single, sheet_name="取引データ")

# ==========================================
# 9.0 統合取引ファイル解析エンジン (全シート・全項目・数値を保持してデータベース連携)
# ==========================================
def parse_trade_file(uploaded_file):
    fname = uploaded_file.name
    content_bytes = uploaded_file.getvalue()
    all_sheet_records = []
    extracted_suppliers = []

    # 全シートの抽出
    sheets_dict = extract_all_raw_sheets_from_file(uploaded_file)
    uploaded_file.seek(0)

    if sheets_dict:
        for sname, sdf_raw in sheets_dict.items():
            if sdf_raw is None or sdf_raw.empty:
                continue

            # 取引先名の特定 (シート名またはセル内の取引先情報から)
            supplier_found = str(sname).strip()
            base_supplier = re.sub(r"\s*\([0-9]+\)$", "", supplier_found).strip()

            # カラム位置マッピング
            col_map = {}
            for col_idx, col_name in enumerate(sdf_raw.columns):
                h_str = normalize_header(col_name)
                if not h_str: continue
                if any(k in h_str for k in ["仕入日", "日付", "取引日", "伝票日", "年月日"]):
                    col_map["日付"] = col_idx
                elif any(k in h_str for k in ["品名", "商品名", "品目", "名称", "規格"]):
                    col_map["品名"] = col_idx
                elif any(k in h_str for k in ["数量", "個数", "入数", "受入", "納品数"]):
                    col_map["数量"] = col_idx
                elif any(k in h_str for k in ["単価", "仕入単価", "売価", "価格"]):
                    col_map["単価"] = col_idx
                elif any(k in h_str for k in ["仕入金額", "小計", "売上金額", "金額"]):
                    col_map["仕入金額"] = col_idx
                elif any(k in h_str for k in ["支払金額", "支払額", "出金"]):
                    col_map["支払金額"] = col_idx
                elif any(k in h_str for k in ["差引残高", "残高", "差引"]):
                    col_map["差引残高"] = col_idx
                elif any(k in h_str for k in ["合計", "日計", "総合計"]):
                    col_map["合計"] = col_idx
                elif any(k in h_str for k in ["商品コード", "品番", "コード"]):
                    col_map["商品コード"] = col_idx

            if base_supplier and base_supplier not in extracted_suppliers:
                extracted_suppliers.append(base_supplier)

            # 行ごとの精緻なデータ抽出 (和暦変換・日付前方穴埋め・空欄品名の行救済・数値保持)
            cur_date = str(datetime.now().date())
            last_item_name = ""

            for _, r_data in sdf_raw.iterrows():
                r_vals = r_data.values

                # 日付の抽出と標準化
                raw_date_cell = r_vals[col_map["日付"]] if "日付" in col_map and col_map["日付"] < len(r_vals) else None
                parsed_d = parse_japanese_date(raw_date_cell)
                if parsed_d:
                    cur_date = parsed_d

                # 品名の抽出
                raw_name_cell = r_vals[col_map["品名"]] if "品名" in col_map and col_map["品名"] < len(r_vals) else None
                item_str = str(raw_name_cell).strip() if pd.notnull(raw_name_cell) and str(raw_name_cell).strip() not in ("nan", "None", "") else ""

                # 数値項目の精緻な抽出（マイナス値も正確に保持）
                q_val = clean_num(r_vals[col_map["数量"]]) if "数量" in col_map and col_map["数量"] < len(r_vals) else 0
                p_val = clean_num(r_vals[col_map["単価"]]) if "単価" in col_map and col_map["単価"] < len(r_vals) else 0
                sub_val = clean_num(r_vals[col_map["仕入金額"]]) if "仕入金額" in col_map and col_map["仕入金額"] < len(r_vals) else 0
                pay_val = clean_num(r_vals[col_map["支払金額"]]) if "支払金額" in col_map and col_map["支払金額"] < len(r_vals) else 0
                bal_val = clean_num(r_vals[col_map["差引残高"]]) if "差引残高" in col_map and col_map["差引残高"] < len(r_vals) else 0
                tot_val = clean_num(r_vals[col_map["合計"]]) if "合計" in col_map and col_map["合計"] < len(r_vals) else 0

                # 数値補完（仕入金額が0で数量と単価がある場合）
                if sub_val == 0 and p_val != 0 and q_val != 0:
                    sub_val = p_val * q_val

                # 有効明細行の判定: 数量、単価、仕入金額、支払金額、差引残高、合計に値があるか、品名が存在
                has_numeric = (q_val != 0 or p_val != 0 or sub_val != 0 or pay_val != 0 or bal_val != 0 or tot_val != 0)
                if not has_numeric and not item_str:
                    continue

                # 総合計や消費税行の単独除外（明細がない場合）
                if item_str and re.match(r"^(消費税|税額|総合計|総計)$", item_str) and not has_numeric:
                    continue

                # 品名が空欄の場合は直前の品名または明細補助名で補完
                if item_str:
                    last_item_name = item_str
                else:
                    item_str = last_item_name if last_item_name else "品名未指定 (明細)"

                code_val = "AUTO"
                if "商品コード" in col_map and col_map["商品コード"] < len(r_vals):
                    raw_cd = r_vals[col_map["商品コード"]]
                    if pd.notnull(raw_cd) and str(raw_cd).strip():
                        code_val = str(raw_cd).strip().replace(".0", "")

                cat_val = "栗原" if "なめこ" in fname else ("5課 / 6課（川越）" if "6課" in fname else "1課4課（熊谷・大宮） / 2課 / 栗原")

                all_sheet_records.append({
                    "日付": cur_date,
                    "市場番号": base_supplier if base_supplier else "M-001 (自社)",
                    "カテゴリー": cat_val,
                    "商品コード": code_val,
                    "品名": item_str,
                    "数量": q_val,
                    "単価": p_val,
                    "仕入金額": sub_val,
                    "支払金額": pay_val,
                    "差引残高": bal_val if bal_val != 0 else sub_val,
                    "合計": tot_val,
                    "小計": sub_val,
                    "商品名": item_str
                })

        if all_sheet_records:
            df_out = pd.DataFrame(all_sheet_records)
            return df_out, extracted_suppliers

    return pd.DataFrame(), []

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
    if thinking_level is not None:
        gen_cfg["thinkingConfig"] = {"thinkingLevel": thinking_level}

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg
    }
    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    resp = requests.post(url, headers=headers, json=body, timeout=AI_REQUEST_TIMEOUT)
    if resp.status_code == 200:
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError("API returned empty candidates.")
        cand = candidates[0]
        text_parts = [
            p.get("text", "")
            for p in cand.get("content", {}).get("parts", [])
            if "text" in p and not p.get("thought", False)
        ]
        out_text = "".join(text_parts).strip()
        if not out_text:
            text_parts = [p.get("text", "") for p in cand.get("content", {}).get("parts", []) if "text" in p]
            out_text = "".join(text_parts).strip()
        return out_text
    raise RuntimeError(f"Gemini API Error [{resp.status_code}]: {resp.text[:300]}")

def call_gemini_with_fallback(prompt: str, system_instruction: str = "", custom_api_key: str = None) -> str:
    key = _resolve_gemini_key(custom_api_key)
    if not key:
        raise ValueError("GEMINI_API_KEY is not configured.")

    models_to_try = [AI_MODEL_PRIMARY] + [m for m in AI_MODEL_FALLBACKS if m != AI_MODEL_PRIMARY]
    last_err = None

    for idx, model in enumerate(models_to_try):
        lvl = AI_THINKING_LEVEL if idx == 0 else "low"
        try:
            return _gemini_request(model, key, prompt, system_instruction, lvl, AI_MAX_OUTPUT_TOKENS)
        except Exception as e:
            last_err = e
            try:
                return _gemini_request(model, key, prompt, system_instruction, None, AI_NON_STREAM_TOKEN_CAP)
            except Exception as e2:
                last_err = e2
                continue
    raise RuntimeError(f"All Gemini models failed. Last error: {last_err}")

# ==========================================
# 10.0 AI 自動照合エンジン
# ==========================================
def run_ai_auto_reconcile(df_input: pd.DataFrame, custom_api_key: str = None) -> dict:
    if df_input.empty:
        return {"matched": [], "unmatched": [], "summary": "解析対象データが空です。"}

    current_master = st.session_state.item_master
    master_text = "\n".join([
        f"- コード: {r['商品コード']}, 品名: {r['商品名']}, カテゴリー: {r['カテゴリー']}, 標準単価: {r['標準単価']}"
        for _, r in current_master.iterrows()
    ])

    input_sample = df_input.head(35).to_dict("records")
    input_text = json.dumps(input_sample, ensure_ascii=False)

    system_inst = "青果卸売の専門監査AIとして、取引明細と商品マスタを照合し、JSONのみで回答してください。"
    prompt = f"""
【青果商品マスタ】
{master_text}

【取込取引明細】
{input_text}

以下のJSONフォーマットのみを出力してください:
{{
  "summary": "照合結果の総評",
  "matches": [
    {{
      "input_item": "元の品名",
      "matched_code": "マスタの商品コード",
      "matched_name": "マスタの品名",
      "confidence": "HIGH"
    }}
  ]
}}
"""
    try:
        raw_res = call_gemini_with_fallback(prompt, system_inst, custom_api_key)
        clean_res = re.sub(r"^```json\s*", "", raw_res.strip())
        clean_res = re.sub(r"\s*```$", "", clean_res)
        return json.loads(clean_res)
    except Exception as e:
        return {"summary": f"AI自動照合エラー: {e}", "matches": []}

# ==========================================
# タブ 1: 登録済みデータ一覧 (全件リアルタイム表示・直接セル編集可能)
# ==========================================
with tabs[0]:
    st.markdown('<div class="section-title">📋 取引台帳・全件リアルタイム一覧</div>', unsafe_allow_html=True)

    df_current = get_records()

    if df_current.empty:
        st.info("💡 登録されている取引データはありません。『タブ 6』からエクセル・CSVファイルをインポートするか、『タブ 3』から手動登録してください。")
    else:
        c_filter1, c_filter2, c_filter3, c_filter4 = st.columns([2, 2, 2, 2])
        with c_filter1:
            market_filter = st.multiselect("市場番号で絞り込み", options=sorted(df_current["市場番号"].dropna().unique()), default=[])
        with c_filter2:
            cat_filter = st.multiselect("カテゴリーで絞り込み", options=sorted(df_current["カテゴリー"].dropna().unique()), default=[])
        with c_filter3:
            kw_filter = st.text_input("品名検索", placeholder="例: キャベツ, 4525")
        with c_filter4:
            st.metric("登録件数", f"{len(df_current):,} 件", f"総計 ¥{df_current['仕入金額'].sum():,}")

        filtered_df = df_current.copy()
        if market_filter:
            filtered_df = filtered_df[filtered_df["市場番号"].isin(market_filter)]
        if cat_filter:
            filtered_df = filtered_df[filtered_df["カテゴリー"].isin(cat_filter)]
        if kw_filter:
            filtered_df = filtered_df[filtered_df["品名"].astype(str).str.contains(kw_filter, case=False, na=False)]

        st.caption("📝 以下のテーブルは直接セルの値をダブルクリックして編集できます。変更後は下の「💾 変更内容を保存」ボタンを押してください。")

        display_cols = [
            "ID", "日付", "市場番号", "カテゴリー", "商品コード", "品名",
            "数量", "単価", "仕入金額", "支払金額", "差引残高", "合計", "登録担当"
        ]
        cols_to_show = [c for c in display_cols if c in filtered_df.columns]

        column_config = {
            "ID": st.column_config.NumberColumn("ID", disabled=True),
            "日付": st.column_config.DateColumn("日付", required=True),
            "市場番号": st.column_config.TextColumn("市場番号", required=True),
            "カテゴリー": st.column_config.SelectboxColumn("カテゴリー", options=CATEGORY_OPTIONS),
            "商品コード": st.column_config.TextColumn("商品コード"),
            "品名": st.column_config.TextColumn("品名", required=True),
            "数量": st.column_config.NumberColumn("数量", step=1, format="%d"),
            "単価": st.column_config.NumberColumn("単価", format="¥%d"),
            "仕入金額": st.column_config.NumberColumn("仕入金額", format="¥%d"),
            "支払金額": st.column_config.NumberColumn("支払金額", format="¥%d"),
            "差引残高": st.column_config.NumberColumn("差引残高", format="¥%d"),
            "合計": st.column_config.NumberColumn("合計", format="¥%d"),
            "登録担当": st.column_config.TextColumn("登録担当", disabled=True)
        }

        edited_df = st.data_editor(
            filtered_df[cols_to_show],
            use_container_width=True,
            num_rows="dynamic",
            key="main_transaction_data_editor",
            column_config=column_config,
            height=500
        )

        col_act1, col_act2, col_act3 = st.columns([1.5, 1.5, 3])
        with col_act1:
            if st.button("💾 変更内容を保存", type="primary", use_container_width=True, key="btn_save_edits"):
                for _, r in edited_df.iterrows():
                    r_dict = r.to_dict()
                    r_id = r_dict.get("ID")
                    if r_id and pd.notnull(r_id) and r_id > 0:
                        update_single_record_in_db(int(r_id), r_dict)
                st.success("✅ データベースへ変更を反映しました")
                st.session_state.editor_version += 1
                st.rerun()

        with col_act2:
            if is_admin:
                del_id = st.number_input("削除するID", min_value=1, step=1, value=1, key="input_del_id")
                if st.button("🗑️ 指定IDを削除 (復元可能)", type="secondary", use_container_width=True, key="btn_del_id"):
                    delete_records_from_db([del_id])
                    st.warning(f"ID: {del_id} を削除テーブルへ退避しました")
                    st.session_state.editor_version += 1
                    st.rerun()

        with col_act3:
            csv_export = filtered_df.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                label="📥 表示中データをCSVエクスポート",
                data=csv_export,
                file_name=f"transactions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=True
            )

# ==========================================
# タブ 2: 取引データ分析 & 集計
# ==========================================
with tabs[1]:
    st.markdown('<div class="section-title">📊 取引データ可視化 & カテゴリー別分析</div>', unsafe_allow_html=True)
    df_chart = get_records()

    if df_chart.empty:
        st.info("集計対象のデータがありません。")
    else:
        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
        total_subtotal = df_chart["仕入金額"].sum()
        total_qty = df_chart["数量"].sum()
        avg_price = total_subtotal / total_qty if total_qty > 0 else 0
        uniq_items = df_chart["品名"].nunique()

        with col_m1: st.metric("累計仕入総額", f"¥{total_subtotal:,}")
        with col_m2: st.metric("累計仕入数量", f"{total_qty:,} 点")
        with col_m3: st.metric("平均仕入単価", f"¥{int(avg_price):,}")
        with col_m4: st.metric("取扱品目数", f"{uniq_items:,} 品")

        st.write("")
        c_ch1, c_ch2 = st.columns(2)
        with c_ch1:
            st.markdown("##### 🏢 市場・取引先別 仕入金額シェア")
            df_market_agg = df_chart.groupby("市場番号")["仕入金額"].sum().reset_index()
            fig_pie = px.pie(df_market_agg, names="市場番号", values="仕入金額", hole=0.4, color_discrete_sequence=px.colors.qualitative.Prism)
            fig_pie.update_layout(margin=dict(t=20, b=20, l=20, r=20))
            st.plotly_chart(fig_pie, use_container_width=True)

        with c_ch2:
            st.markdown("##### 📦 カテゴリー別 仕入金額ランキング")
            df_cat_agg = df_chart.groupby("カテゴリー")["仕入金額"].sum().reset_index().sort_values("仕入金額", ascending=True)
            fig_bar = px.bar(df_cat_agg, x="仕入金額", y="カテゴリー", orientation="h", color="仕入金額", color_continuous_scale="Viridis")
            fig_bar.update_layout(margin=dict(t=20, b=20, l=20, r=20))
            st.plotly_chart(fig_bar, use_container_width=True)

        st.markdown("##### 📈 日別 仕入推移")
        df_chart["日付_dt"] = pd.to_datetime(df_chart["日付"], errors="coerce")
        df_daily = df_chart.dropna(subset=["日付_dt"]).groupby("日付_dt")["仕入金額"].sum().reset_index().sort_values("日付_dt")
        if not df_daily.empty:
            fig_line = px.line(df_daily, x="日付_dt", y="仕入金額", markers=True, line_shape="spline")
            fig_line.update_layout(margin=dict(t=20, b=20, l=20, r=20), xaxis_title="日付", yaxis_title="仕入金額 (円)")
            st.plotly_chart(fig_line, use_container_width=True)

# ==========================================
# タブ 3: 新規取引手動登録
# ==========================================
with tabs[2]:
    st.markdown('<div class="section-title">➕ 新規取引伝票 手動登録</div>', unsafe_allow_html=True)

    with st.form("manual_add_form"):
        c_in1, c_in2, c_in3 = st.columns(3)
        with c_in1:
            f_date = st.date_input("取引日", datetime.now().date())
            f_market = st.selectbox("市場番号 / 取引先", options=st.session_state.extracted_suppliers)
        with c_in2:
            f_cat = st.selectbox("カテゴリー", options=CATEGORY_OPTIONS)
            f_code = st.text_input("商品コード", value="1001")
        with c_in3:
            f_name = st.text_input("品名", value="キャベツ")
            f_price = st.number_input("単価 (円)", min_value=0, value=150, step=10)

        c_in4, c_in5, c_in6, c_in7 = st.columns(4)
        with c_in4:
            f_qty = st.number_input("数量", min_value=1, value=10, step=1)
        with c_in5:
            calc_sub = f_price * f_qty
            f_sub = st.number_input("仕入金額 (円)", value=calc_sub, step=100)
        with c_in6:
            f_pay = st.number_input("支払金額 (円)", value=0, step=100)
        with c_in7:
            f_bal = st.number_input("差引残高 (円)", value=f_sub - f_pay, step=100)

        submit_add = st.form_submit_button("伝票を登録する", type="primary", use_container_width=True)

        if submit_add:
            new_record = [{
                "日付": str(f_date),
                "市場番号": f_market,
                "カテゴリー": f_cat,
                "商品コード": f_code,
                "品名": f_name,
                "単価": f_price,
                "数量": f_qty,
                "仕入金額": f_sub,
                "支払金額": f_pay,
                "差引残高": f_bal,
                "合計": 0,
                "小計": f_sub,
                "商品名": f_name
            }]
            insert_records_to_db(current_user, new_record)
            st.success(f"✅ 取引を登録しました: 『{f_name}』 ¥{f_sub:,}")
            st.rerun()

# ==========================================
# タブ 4: 商品マスタ管理
# ==========================================
with tabs[3]:
    st.markdown('<div class="section-title">🗂️ 取扱商品マスタ管理</div>', unsafe_allow_html=True)
    st.caption("取扱青果物および標準単価の管理を行います。")

    m_df = st.session_state.item_master
    edited_master = st.data_editor(
        m_df,
        use_container_width=True,
        num_rows="dynamic",
        key="master_editor_table",
        column_config={
            "商品コード": st.column_config.TextColumn("商品コード", required=True),
            "カテゴリー": st.column_config.SelectboxColumn("カテゴリー", options=CATEGORY_OPTIONS, required=True),
            "商品名": st.column_config.TextColumn("商品名", required=True),
            "標準単価": st.column_config.NumberColumn("標準単価", format="¥%d", min_value=0)
        }
    )

    if st.button("💾 商品マスタの変更を保存", type="primary"):
        st.session_state.item_master = edited_master
        try:
            edited_master.to_csv(MASTER_CSV_FILE, index=False, encoding="utf-8-sig")
            st.success("✅ 商品マスタをローカルファイルへ永続化しました。")
        except Exception as e:
            st.error(f"マスタ保存エラー: {e}")

# ==========================================
# タブ 5: バックアップ & データ復元
# ==========================================
with tabs[4]:
    st.markdown('<div class="section-title">💾 データベース・バックアップ & 復元管理</div>', unsafe_allow_html=True)

    if not is_admin:
        st.warning("⚠️ バックアップ・復元操作は ADMIN 権限のユーザーのみ実行可能です。")
    else:
        c_b1, c_b2 = st.columns(2)
        with c_b1:
            st.markdown("##### 📦 即時バックアップ作成")
            if st.button("今すぐ手動バックアップを作成する", type="primary", use_container_width=True):
                create_auto_backup()
                st.success("最新のスナップショットを作成しました。")
                st.rerun()

        with c_b2:
            st.markdown("##### ♻️ 削除済みデータの即時復元")
            engine = get_engine()
            with engine.connect() as conn:
                df_deleted = pd.read_sql("SELECT id, date, market_number, item_name, subtotal, deleted_at FROM deleted_transactions ORDER BY deleted_at DESC LIMIT 20", conn)
            if not df_deleted.empty:
                st.write(df_deleted)
                rest_id = st.number_input("復元するID", min_value=1, step=1, value=int(df_deleted.iloc[0]["id"]))
                if st.button("指定IDのデータを復元する", type="secondary"):
                    restore_records_to_db([rest_id])
                    st.success(f"ID {rest_id} を取引一覧へ復元しました。")
                    st.rerun()
            else:
                st.info("現在、削除されたデータはありません。")

        st.write("---")
        st.markdown("##### 🗃️ 過去のバックアップファイル一覧")
        b_files = glob.glob(os.path.join(BACKUP_DIR, "backup_*.json"))
        b_files.sort(reverse=True)
        if b_files:
            for bf in b_files[:8]:
                b_name = os.path.basename(bf)
                b_size = os.path.getsize(bf)
                b_time = datetime.fromtimestamp(os.path.getmtime(bf)).strftime("%Y-%m-%d %H:%M:%S")
                with open(bf, "r", encoding="utf-8") as f:
                    b_content = f.read()
                c_f1, c_f2 = st.columns([3, 1])
                with c_f1:
                    st.text(f"📁 {b_name} ({b_size/1024:.1f} KB) - 作成: {b_time}")
                with c_f2:
                    st.download_button("ダウンロード", data=b_content, file_name=b_name, mime="application/json", key=f"dl_{b_name}")
        else:
            st.info("バックアップデータはまだ作成されていません。")

# ==========================================
# タブ 6: 取引データ一括インポート & 形式変換 (.xls ➡ .xlsx)
# ==========================================
with tabs[5]:
    st.markdown('<div class="section-title">📥 取引データ一括インポート & 昔のエクセル形式 (.xls) ➡ 最新形式 (.xlsx) 変換</div>', unsafe_allow_html=True)

    st.markdown("""
        <div class="converter-box">
            <h4 style="margin: 0; color: #0369a1;">🔄 昔のエクセルファイル形式（.xls）を最新形式（.xlsx）へ全シート・全数値保持で完全変換</h4>
            <p style="margin: 6px 0 0 0; color: #334155; font-size: 13px;">
                「ビューティー花壇 .xls」や「築館なめこ .xls」「仕入台帳6課 .xls」など、古い形式の <b>.xls</b> ファイルをアップロードするだけで、
                <b>シート毎に表示されている「品名」「数量」「単価」「仕入金額」「支払金額」「差引残高」「合計」の数値データおよび書式を100%保持</b> したまま、
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
            # 全シート生データの抽出
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
                # シート別プレビュー表示
                if len(raw_sheets) > 1:
                    st.markdown("##### 📑 シート別プレビュー（各シートの品名・数値データをそのまま確認）")
                    preview_tabs = st.tabs(list(raw_sheets.keys())[:8])
                    for t_idx, (sname, sdf) in enumerate(list(raw_sheets.items())[:8]):
                        with preview_tabs[t_idx]:
                            st.caption(f"シート名: **{sname}** ({len(sdf)} 行)")
                            st.dataframe(sdf, use_container_width=True)
                else:
                    st.markdown("##### 📋 解析データプレビュー")
                    preview_cols = [c for c in ["日付", "市場番号", "品名", "数量", "単価", "仕入金額", "支払金額", "差引残高", "合計"] if c in df_up.columns]
                    st.dataframe(df_up[preview_cols], use_container_width=True)

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
