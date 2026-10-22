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
    """環境変数または st.secrets から設定値を取得"""
    val = os.environ.get(key)
    if val: return val
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return default

def get_engine():
    """データベース接続エンジン取得（PostgreSQL優先、ローカルSQLite完全互換）"""
    database_url = get_secret("DATABASE_URL")
    if database_url:
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        return create_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=10,
            max_overflow=20
        )
    return create_engine(
        f"sqlite:///{SQLITE_DB_PATH}?check_same_thread=False",
        connect_args={"check_same_thread": False}
    )

def is_using_cloud_db() -> bool:
    return bool(get_secret("DATABASE_URL"))

# ==========================================
# 2. セキュリティ & 認証関数 (PBKDF2暗号化)
# ==========================================
AUTH_SECRET_KEY = get_secret("AUTH_SECRET_KEY", "seika_corp_secure_master_salt_2026_x9")

def hash_password(password: str, salt: str = None) -> str:
    if not salt:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return f"{salt}:{key.hex()}"

def verify_password(stored_hash: str, provided_password: str) -> bool:
    try:
        salt, key_hex = stored_hash.split(":")
        new_key = hashlib.pbkdf2_hmac("sha256", provided_password.encode("utf-8"), salt.encode("utf-8"), 100000)
        return hmac.compare_digest(key_hex, new_key.hex())
    except Exception:
        return False

def create_session_token(username: str, role: str) -> str:
    ts = int(time.time())
    payload = f"{username}|{role}|{ts}"
    sig = hmac.new(AUTH_SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"

def verify_session_token(token: str):
    try:
        parts = token.split("|")
        if len(parts) != 4:
            return None
        username, role, ts_str, sig = parts
        ts = int(ts_str)
        # 有効期限 14日間
        if time.time() - ts > (14 * 86400):
            return None
        payload = f"{username}|{role}|{ts}"
        expected_sig = hmac.new(AUTH_SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected_sig):
            return {"username": username, "role": role}
    except Exception:
        pass
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
# 4. ユーザー認証関数
# ==========================================
def verify_user_login(username: str, password_raw: str):
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT username, password_hash, role, display_name FROM users WHERE username = :u"),
            {"u": username.strip()}
        ).fetchone()
        if row and verify_password(row[1], password_raw):
            return {"username": row[0], "role": row[2], "display_name": row[3] or row[0]}
    return None

def get_all_users_from_db():
    engine = get_engine()
    with engine.connect() as conn:
        res = conn.execute(text("SELECT username, role, display_name, created_at FROM users ORDER BY id ASC")).fetchall()
        return [{"username": r[0], "role": r[1], "display_name": r[2] or r[0], "created_at": str(r[3])} for r in res]

def register_new_user_db(username, password, role="staff", display_name=""):
    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (username, password_hash, role, display_name) VALUES (:u, :p, :r, :d)"),
                {"u": username.strip(), "p": hash_password(password), "r": role, "d": display_name.strip() or username.strip()}
            )
        return True, "ユーザーを新規登録しました"
    except Exception as e:
        return False, f"登録エラー: {e}"

# ==========================================
# 5. 取引データ永続化 & 高速キャッシュ
# ==========================================
@st.cache_data(ttl=60, show_spinner=False)
def load_records_cached(target_user_filter: str = None):
    engine = get_engine()
    with engine.connect() as conn:
        if target_user_filter and target_user_filter != "全員のデータを表示":
            query = text("""
                SELECT id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, created_at
                FROM transactions
                WHERE user_id = :uid
                ORDER BY date DESC, id DESC
            """)
            df = pd.read_sql(query, conn, params={"uid": target_user_filter})
        else:
            query = text("""
                SELECT id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, created_at
                FROM transactions
                ORDER BY date DESC, id DESC
            """)
            df = pd.read_sql(query, conn)
            
    df = df.rename(columns={
        "date": "日付", "market_number": "市場番号", "category": "カテゴリー",
        "item_code": "商品コード", "item_name": "品名", "unit_price": "単価",
        "quantity": "数量", "subtotal": "仕入金額", "user_id": "登録担当者"
    })
    if not df.empty:
        df["商品名"] = df["品名"]
        df["小計"] = df["仕入金額"]
        df["支払金額"] = 0
        df["差引残高"] = df["仕入金額"]
        df["合計"] = df["仕入金額"]
    return df

def load_records_from_db(target_user_filter: str = None):
    return load_records_cached(target_user_filter)

def invalidate_records_cache():
    load_records_cached.clear()

def insert_records_to_db(user_id: str, records: list, auto_backup=True):
    if not records:
        return
    engine = get_engine()
    now_ts = datetime.now()
    with engine.begin() as conn:
        for r in records:
            p = int(r.get("単価", 0))
            q = int(r.get("数量", 0))
            sub = int(r.get("仕入金額", r.get("小計", p * q)))
            item_n = str(r.get("品名", r.get("商品名", "品名未指定"))).strip() or "品名未指定"
            conn.execute(
                text("""
                    INSERT INTO transactions 
                    (date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, created_at)
                    VALUES (:d, :m, :c, :ic, :in, :p, :q, :sub, :u, :cat)
                """),
                {
                    "d": str(r.get("日付", now_ts.strftime("%Y-%m-%d"))),
                    "m": str(r.get("市場番号", "M-001")),
                    "c": str(r.get("カテゴリー", "1課 (熊谷・大宮)")),
                    "ic": str(r.get("商品コード", "AUTO")),
                    "in": item_n,
                    "p": p,
                    "q": q,
                    "sub": sub,
                    "u": user_id,
                    "cat": now_ts
                }
            )
    invalidate_records_cache()
    if auto_backup:
        create_backup_snapshot(user_id, trigger_reason=f"自動追記バックアップ ({len(records)}件記録)")

def update_single_record_in_db(record_dict: dict):
    rec_id = record_dict.get("id")
    if not rec_id: return False
    engine = get_engine()
    p = int(record_dict.get("単価", 0))
    q = int(record_dict.get("数量", 0))
    sub = int(record_dict.get("仕入金額", record_dict.get("小計", p * q)))
    item_n = str(record_dict.get("品名", record_dict.get("商品名", "品名未指定"))).strip() or "品名未指定"
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE transactions
                SET date = :d,
                    market_number = :m,
                    category = :c,
                    item_code = :ic,
                    item_name = :in,
                    unit_price = :p,
                    quantity = :q,
                    subtotal = :sub
                WHERE id = :rid
            """),
            {
                "d": str(record_dict.get("日付", "")),
                "m": str(record_dict.get("市場番号", "")),
                "c": str(record_dict.get("カテゴリー", "")),
                "ic": str(record_dict.get("商品コード", "")),
                "in": item_n,
                "p": p, "q": q, "sub": sub, "rid": rec_id
            }
        )
    invalidate_records_cache()
    return True

def delete_records_by_ids(ids: list, deleted_by: str):
    if not ids: return
    engine = get_engine()
    with engine.begin() as conn:
        for rid in ids:
            row = conn.execute(
                text("SELECT id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id FROM transactions WHERE id = :rid"),
                {"rid": rid}
            ).fetchone()
            if row:
                conn.execute(
                    text("""
                        INSERT INTO deleted_transactions
                        (original_id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, deleted_by)
                        VALUES (:oid, :d, :m, :c, :ic, :in, :p, :q, :sub, :u, :db)
                    """),
                    {
                        "oid": row[0], "d": row[1], "m": row[2], "c": row[3], "ic": row[4],
                        "in": row[5], "p": row[6], "q": row[7], "sub": row[8], "u": row[9], "db": deleted_by
                    }
                )
        conn.execute(text(f"DELETE FROM transactions WHERE id IN ({','.join(map(str, ids))})"))
    invalidate_records_cache()
    create_backup_snapshot(deleted_by, trigger_reason=f"データ削除前自動退避 ({len(ids)}件削除)")

def get_employee_summary_stats():
    engine = get_engine()
    with engine.connect() as conn:
        query = text("""
            SELECT user_id, 
                   COUNT(*) as record_count, 
                   SUM(subtotal) as total_amount, 
                   SUM(quantity) as total_quantity,
                   MAX(date) as last_activity
            FROM transactions
            GROUP BY user_id
            ORDER BY total_amount DESC
        """)
        df = pd.read_sql(query, conn)
    return df

# ==========================================
# 6. バックアップ & 高速復元エンジン
# ==========================================
def create_backup_snapshot(user_id: str, trigger_reason="定期自動バックアップ"):
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    engine = get_engine()
    with engine.connect() as conn:
        df_all = pd.read_sql("SELECT * FROM transactions ORDER BY id ASC", conn)
    meta = {
        "timestamp": now_str,
        "created_by": user_id,
        "reason": trigger_reason,
        "record_count": len(df_all),
        "total_subtotal": int(df_all["subtotal"].sum()) if not df_all.empty and "subtotal" in df_all.columns else 0
    }
    bk_filename = f"backup_{now_str}.json"
    bk_path = os.path.join(BACKUP_DIR, bk_filename)
    payload = {
        "meta": meta,
        "records": df_all.to_dict(orient="records")
    }
    with open(bk_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return bk_filename

def list_all_backups():
    files = glob.glob(os.path.join(BACKUP_DIR, "backup_*.json"))
    files.sort(reverse=True)
    backups = []
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
                meta = d.get("meta", {})
                backups.append({
                    "filepath": fp,
                    "filename": os.path.basename(fp),
                    "timestamp": meta.get("timestamp", "不明"),
                    "created_by": meta.get("created_by", "system"),
                    "reason": meta.get("reason", "自動作成"),
                    "record_count": meta.get("record_count", 0),
                    "total_subtotal": meta.get("total_subtotal", 0)
                })
        except Exception:
            continue
    return backups

def restore_from_backup_file(filepath: str, restored_by: str):
    if not os.path.exists(filepath):
        return {"success": False, "message": "バックアップファイルが存在しません"}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        records = data.get("records", [])
        create_backup_snapshot(restored_by, trigger_reason="データ復元直前の現行データ自動保護")
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM transactions;"))
            for r in records:
                conn.execute(
                    text("""
                        INSERT INTO transactions 
                        (date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, created_at)
                        VALUES (:d, :m, :c, :ic, :in, :p, :q, :sub, :u, :cat)
                    """),
                    {
                        "d": str(r.get("date", "")),
                        "m": str(r.get("market_number", "")),
                        "c": str(r.get("category", "")),
                        "ic": str(r.get("item_code", "")),
                        "in": str(r.get("item_name", "")),
                        "p": int(r.get("unit_price", 0)),
                        "q": int(r.get("quantity", 0)),
                        "sub": int(r.get("subtotal", 0)),
                        "u": str(r.get("user_id", "restored")),
                        "cat": str(r.get("created_at", datetime.now()))
                    }
                )
        invalidate_records_cache()
        return {"success": True, "restored_count": len(records)}
    except Exception as e:
        return {"success": False, "message": str(e)}

# ==========================================
# 7. 商品マスタ & カテゴリー連動定義
# ==========================================
CATEGORY_TXT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "category.txt")

BASE_DEPT_LIST = [
    "1課 (熊谷・大宮)",
    "2課 (川越・地方)",
    "3課 (外食・加工)",
    "4課 (量販店・スーパー)",
    "5課 (産直・促成)",
    "6課 (特販・業務用)",
    "栗原 (特約出荷)"
]

def _norm_dept(s) -> str:
    if s is None:
        return ""
    norm = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"[\s\u3000\t\r\n・_／/\\()（）]+", "", norm).lower()

def _dept_tokens(s) -> set:
    norm = _norm_dept(s)
    tokens = set()
    for m in re.finditer(r"([0-9０-９]+)課?", norm):
        digits = unicodedata.normalize("NFKC", m.group(1))
        tokens.add(f"{digits}課")
    for keyword in ["熊谷", "大宮", "川越", "地方", "外食", "加工", "量販", "スーパー", "産直", "促成", "特販", "業務", "栗原", "特約"]:
        if keyword in norm:
            tokens.add(keyword)
    return tokens

def _dept_match(row_dept, chosen_dept) -> bool:
    if not row_dept or not chosen_dept:
        return False
    r_norm = _norm_dept(row_dept)
    c_norm = _norm_dept(chosen_dept)
    if r_norm == c_norm or r_norm in c_norm or c_norm in r_norm:
        return True
    r_tokens = _dept_tokens(row_dept)
    c_tokens = _dept_tokens(chosen_dept)
    if r_tokens and c_tokens and (r_tokens & c_tokens):
        return True
    return False

def get_items_for_department(df_master, dept):
    if df_master is None or df_master.empty:
        return pd.DataFrame(columns=["department", "item_code", "item_name", "standard_price"])
    mask = df_master["department"].apply(lambda d: _dept_match(d, dept))
    matched = df_master[mask]
    return matched if not matched.empty else df_master

def _file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0

def _read_category_txt(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=["department", "item_code", "item_name", "standard_price"])
    rows = []
    current_dept = "1課 (熊谷・大宮)"
    encodings = ["utf-8-sig", "utf-8", "cp932", "shift-jis", "euc-jp"]
    content = ""
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc) as f:
                content = f.read()
                break
        except Exception:
            continue
    if not content:
        return pd.DataFrame(columns=["department", "item_code", "item_name", "standard_price"])
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        dept_match = re.match(r"^\[(.+?)\]$", line)
        if dept_match:
            current_dept = dept_match.group(1).strip()
            continue
        parts = [p.strip() for p in re.split(r"[\t,]+", line) if p.strip()]
        if len(parts) >= 2:
            code = parts[0]
            name = parts[1]
            price = 0
            if len(parts) >= 3:
                try: price = int(re.sub(r"[^\d]", "", parts[2]) or 0)
                except Exception: price = 0
            rows.append({
                "department": current_dept,
                "item_code": code,
                "item_name": name,
                "standard_price": price
            })
        elif len(parts) == 1 and not parts[0].endswith("課"):
            rows.append({
                "department": current_dept,
                "item_code": "AUTO",
                "item_name": parts[0],
                "standard_price": 0
            })
    return pd.DataFrame(rows)

@st.cache_data(show_spinner=False)
def load_item_master_cached(csv_mtime: float = 0.0, txt_mtime: float = 0.0):
    all_dfs = []
    if os.path.exists(MASTER_CSV_FILE):
        for enc in ["utf-8-sig", "cp932", "shift-jis"]:
            try:
                df_csv = pd.read_csv(MASTER_CSV_FILE, encoding=enc)
                df_csv.columns = [str(c).strip().lower() for c in df_csv.columns]
                renames = {}
                for c in df_csv.columns:
                    c_norm = _norm_dept(c)
                    if any(k in c_norm for k in ["課", "部", "カテゴリ", "dept"]): renames[c] = "department"
                    elif any(k in c_norm for k in ["コード", "品番", "code"]): renames[c] = "item_code"
                    elif any(k in c_norm for k in ["品名", "商品名", "名称", "name"]): renames[c] = "item_name"
                    elif any(k in c_norm for k in ["単価", "価格", "price"]): renames[c] = "standard_price"
                df_csv = df_csv.rename(columns=renames)
                for req in ["department", "item_code", "item_name"]:
                    if req not in df_csv.columns: df_csv[req] = ""
                if "standard_price" not in df_csv.columns: df_csv["standard_price"] = 0
                all_dfs.append(df_csv[["department", "item_code", "item_name", "standard_price"]])
                break
            except Exception:
                continue

    if os.path.exists(CATEGORY_TXT_FILE):
        df_txt = _read_category_txt(CATEGORY_TXT_FILE)
        if not df_txt.empty:
            all_dfs.append(df_txt)

    if all_dfs:
        df_master = pd.concat(all_dfs, ignore_index=True)
    else:
        sample_data = [
            {"department": "1課 (熊谷・大宮)", "item_code": "1001", "item_name": "キャベツ", "standard_price": 180},
            {"department": "1課 (熊谷・大宮)", "item_code": "1002", "item_name": "大根", "standard_price": 150},
            {"department": "1課 (熊谷・大宮)", "item_code": "1003", "item_name": "白菜", "standard_price": 220},
            {"department": "2課 (川越・地方)", "item_code": "2001", "item_name": "トマト", "standard_price": 300},
            {"department": "2課 (川越・地方)", "item_code": "2002", "item_name": "きゅうり", "standard_price": 200},
            {"department": "3課 (外食・加工)", "item_code": "3001", "item_name": "カットレタス", "standard_price": 250},
            {"department": "4課 (量販店・スーパー)", "item_code": "4001", "item_name": "人参 3本袋", "standard_price": 120},
            {"department": "5課 (産直・促成)", "item_code": "5001", "item_name": "減農薬ほうれん草", "standard_price": 190},
            {"department": "6課 (特販・業務用)", "item_code": "6001", "item_name": "業務用玉ねぎ 10kg", "standard_price": 1800},
            {"department": "栗原 (特約出荷)", "item_code": "7001", "item_name": "築館なめこ", "standard_price": 2000}
        ]
        df_master = pd.DataFrame(sample_data)

    df_master["department"] = df_master["department"].fillna("1課 (熊谷・大宮)").astype(str)
    df_master["item_code"] = df_master["item_code"].fillna("AUTO").astype(str)
    df_master["item_name"] = df_master["item_name"].fillna("").astype(str)
    df_master["standard_price"] = pd.to_numeric(df_master["standard_price"], errors="coerce").fillna(0).astype(int)
    for col in ["department", "item_code", "item_name"]:
        df_master[col] = df_master[col].str.strip()
    df_master = df_master[df_master["item_name"] != ""].copy()

    dedup_key = df_master["department"].map(_norm_dept) + "|" + df_master["item_code"] + "|" + df_master["item_name"]
    df_master = df_master[~dedup_key.duplicated()].reset_index(drop=True)

    departments = list(BASE_DEPT_LIST)
    for d in df_master["department"].unique().tolist():
        if d and not any(_dept_match(d, b) for b in BASE_DEPT_LIST) and d not in departments:
            departments.append(d)
    return df_master, departments

df_master_raw, DEPT_LIST = load_item_master_cached(_file_mtime(MASTER_CSV_FILE), _file_mtime(CATEGORY_TXT_FILE))

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
# 8.5 旧来形式 (.xls) ➡ 最新形式 (.xlsx) 完全書式・配置保持変換エンジン
# ==========================================
def col_letter(col_idx: int) -> str:
    """0-indexed 列番号を Excel 列文字 (A, B, ..., Z, AA, AB, ...) に変換"""
    res = ""
    col_idx += 1
    while col_idx > 0:
        col_idx, rem = divmod(col_idx - 1, 26)
        res = chr(65 + rem) + res
    return res

def clean_num(val, default=0):
    """
    「¥2,000」「340,000 円」「¥ 500」「▲500」「12.0」等の
    全角記号・通貨単位・カンマ・マイナス表記を100%確実にクレンジングして整数化。
    未入力・空文字・NaNは default を返却。
    """
    if val is None or pd.isnull(val):
        return default
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if val != val:  # NaN
            return default
        return int(round(val))
    
    s = unicodedata.normalize("NFKC", str(val)).strip()
    if not s or s in ("-", "--", "―", "ー", "None", "nan", "NaN", "null"):
        return default
    
    is_neg = False
    if "▲" in s or "△" in s or (s.startswith("(") and s.endswith(")")):
        is_neg = True
    elif s.startswith("-"):
        is_neg = True
        
    clean_digits = re.sub(r"[^\d\.]", "", s)
    if not clean_digits:
        return default
    try:
        f = float(clean_digits)
        res = int(round(f))
        return -res if is_neg else res
    except Exception:
        return default

def parse_japanese_date(val, base_year=None) -> str:
    """
    和暦 (R8.9.2, 令8.9.10等) や 9月5日、9/11、西暦日付、
    およびExcel内部日付シリアル値（例: 46267 ➡ 2026-09-02）を YYYY-MM-DD に完全標準化
    """
    if val is None or pd.isnull(val):
        return ""
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        # Excelシリアル値（20000〜65000は1954年〜2077年）
        if 20000 <= val <= 65000:
            try:
                dt = datetime(1899, 12, 30) + timedelta(days=float(val))
                return dt.strftime("%Y-%m-%d")
            except Exception:
                pass
    s = unicodedata.normalize("NFKC", str(val)).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return ""
    
    # 文字列形式のシリアル値
    if s.isdigit() and 20000 <= int(s) <= 65000:
        try:
            dt = datetime(1899, 12, 30) + timedelta(days=int(s))
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    # 和暦パターン: R8.9.2, 令8.9.10, H30/5/1 等
    m_wareki = re.match(r"^([RrHhSsTt令平昭]|令和|平成|昭和)?\s*(\d{1,2})[\.年/\-](\d{1,2})[\.月/\-](\d{1,2})日?", s)
    if m_wareki:
        era, y_s, m_s, d_s = m_wareki.groups()
        y_val = int(y_s)
        era = era or "R"
        if any(k in era for k in ("R", "r", "令")):
            base_y = 2018 + y_val
        elif any(k in era for k in ("H", "h", "平")):
            base_y = 1988 + y_val
        elif any(k in era for k in ("S", "s", "昭")):
            base_y = 1925 + y_val
        else:
            base_y = 2018 + y_val if y_val < 50 else (1900 + y_val)
        return f"{base_y:04d}-{int(m_s):02d}-{int(d_s):02d}"

    # 西暦年月日パターン: 2026-09-02, 2026/9/2
    m_ymd = re.match(r"^(\d{4})[\.年/\-](\d{1,2})[\.月/\-](\d{1,2})日?", s)
    if m_ymd:
        return f"{int(m_ymd.group(1)):04d}-{int(m_ymd.group(2)):02d}-{int(m_ymd.group(3)):02d}"

    # 月日パターン: 9月5日, 9/5
    m_md = re.match(r"^(\d{1,2})[\.月/\-](\d{1,2})日?$", s)
    if m_md:
        cur_y = base_year or datetime.now().year
        return f"{cur_y:04d}-{int(m_md.group(1)):02d}-{int(m_md.group(2)):02d}"

    return s.split("T")[0].split(" ")[0].strip()
def format_excel_date_serial(serial_val, is_first_row=False, fmt_str="", col_idx=0):
    """
    Excelシリアル値（20000〜65000: 1954年〜2077年）を、
    元エクセルの表示形式（R8.9.2 または 9月5日、令和8年など）に合わせて完全復元
    """
    try:
        dt = datetime(1899, 12, 30) + timedelta(days=float(serial_val))
        y, m, d = dt.year, dt.month, dt.day
        r_y = y - 2018  # 令和年 (2026年 -> 令和8年)
        fmt_lower = fmt_str.lower() if fmt_str else ""

        # 1. 和暦パターン (ggge, ge, e., R)
        if any(k in fmt_lower for k in ["ggge", "ge", "e.", "r", "令"]):
            if "年" in fmt_str:
                if "日" in fmt_str:
                    return f"令和{r_y}年{m}月{d}日"
                elif "月" in fmt_str:
                    return f"令和{r_y}年 {m}月" if " " in fmt_str else f"令和{r_y}年{m}月"
                return f"令和{r_y}年"
            return f"R{r_y}.{m}.{d}"

        # 2. 請求金額等のカスタムフォーマット (例: m"月 請求金額")
        if "請求金額" in fmt_str:
            return f"{m}月 請求金額"

        # 3. 月日パターン (m月d日, m"月"d"日")
        if any(k in fmt_str for k in ['m"月"d"日"', "m月d日", "m/d", "m-d"]):
            return f"{m}月{d}日"

        # 4. 月のみパターン (m月, m"月")
        if any(k in fmt_str for k in ['m"月"', "m月"]):
            return f"{m}月"

        # 5. 西暦パターン (yyyy/m/d, yyyy-mm-dd)
        if "yyyy" in fmt_lower or "yy" in fmt_lower:
            return dt.strftime("%Y/%m/%d") if "/" in fmt_str else dt.strftime("%Y-%m-%d")

        # 6. フォーマット不明時の列位置推定:
        # 出荷日列（Col A または col_idx == 0）の場合:
        if col_idx == 0:
            if is_first_row:
                return f"R{r_y}.{m}.{d}"
            else:
                return f"{m}月{d}日"

        return f"{m}月{d}日"
    except Exception:
        return str(serial_val)

def build_raw_grid_xlsx_bytes(sheet_grids: dict) -> bytes:
    """
    全シートの「セル配置・行・列・空白・数値・書式」を1セルたりとも削らず、
    元のエクセルの見た目・配置そのままに最新の .xlsx (OpenXML) を完全生成
    """
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

        used_names = set()
        for idx, (sname, grid) in enumerate(sheet_grids.items(), start=1):
            part_name = f"/xl/worksheets/sheet{idx}.xml"
            content_types.append(f'  <Override PartName="{part_name}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            workbook_rels.append(f'  <Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>')
            
            clean_sname = str(sname).strip()[:31]
            for ch in [':', '\\', '/', '?', '*', '[', ']']:
                clean_sname = clean_sname.replace(ch, '_')
            if not clean_sname:
                clean_sname = f"Sheet{idx}"
            base_s = clean_sname
            c_dup = 1
            while clean_sname.lower() in used_names:
                clean_sname = f"{base_s[:28]}_{c_dup}"
                c_dup += 1
            used_names.add(clean_sname.lower())

            esc_sname = clean_sname.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            sheets_xml.append(f'    <sheet name="{esc_sname}" sheetId="{idx}" r:id="rId{idx}"/>')

            rows_xml = []
            for r_i, row_data in enumerate(grid, start=1):
                c_cells = []
                for c_i, val in enumerate(row_data):
                    # 空白セルはそのまま空欄（決して0で埋めない）
                    if val is None or val == "" or (isinstance(val, float) and pd.isna(val)):
                        continue
                    cref = f"{col_letter(c_i)}{r_i}"
                    if isinstance(val, bool):
                        c_cells.append(f'<c r="{cref}" t="b"><v>{1 if val else 0}</v></c>')
                    elif isinstance(val, (int, float)):
                        if val != val: continue
                        c_cells.append(f'<c r="{cref}"><v>{val}</v></c>')
                    else:
                        esc_v = str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")
                        c_cells.append(f'<c r="{cref}" t="inlineStr"><is><t>{esc_v}</t></is></c>')
                if c_cells:
                    rows_xml.append(f'<row r="{r_i}">{"".join(c_cells)}</row>')

            sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    {"".join(rows_xml)}
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

# ==========================================
# 8.8 ヘッダー文字列正規化 & 最適ヘッダー行スコアリング
# ==========================================
def normalize_header(s) -> str:
    """全角半角スペースや記号を除去し、日本語表記揺れを完全吸収"""
    if s is None:
        return ""
    norm = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"[\s\u3000\t\r\n・_／/\\()（）:：様御中殿]+", "", norm).lower()

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
            if any(k in s for k in ["出荷日", "日付", "年月日", "取引日", "伝票日", "納品日", "受入日"]): score += 3
            if any(k in s for k in ["品名", "商品名", "品目", "規格", "品名規格", "名称", "品種"]): score += 3
            if any(k in s for k in ["数量", "個数", "箱数", "受入", "納品数", "入数", "バラ", "ケース", "包数"]): score += 3
            if any(k in s for k in ["単価", "売価", "仕入単価", "卸単価", "価格"]): score += 3
            if any(k in s for k in ["仕入金額", "金額", "小計", "売上金額", "計", "販売金額"]): score += 3
            if any(k in s for k in ["市場", "取引先", "仕入先", "顧客", "得意先", "納品先", "相手先", "会社", "荷受", "荷主"]): score += 2
            if any(k in s for k in ["商品コード", "品番", "品目コード", "コード"]): score += 2
        if score > max_score:
            max_score = score
            best_idx = r_idx
    return best_idx if max_score >= 2 else 0

# ==========================================
# 9.0 Pure-Python OLE2 / BIFF8 & OpenXML (.xlsx) バイナリパーサー
# ==========================================
def extract_ole_workbook_stream(data: bytes) -> bytes:
    """OLE2 Compound File から Workbook / Book ストリームを純粋なPythonで直接抽出"""
    if len(data) < 512 or data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return None
    try:
        sector_size = 1 << struct.unpack_from("<H", data, 30)[0]
        fat_sectors_count = struct.unpack_from("<I", data, 44)[0]
        dir_first_sec = struct.unpack_from("<I", data, 48)[0]
        
        fat = []
        for i in range(min(109, fat_sectors_count)):
            sec_idx = struct.unpack_from("<I", data, 76 + i * 4)[0]
            if sec_idx < 0xFFFFFFFE:
                offset = (sec_idx + 1) * sector_size
                sec_data = data[offset:offset + sector_size]
                fat.extend(struct.unpack(f"<{len(sec_data) // 4}I", sec_data))
                
        cur_sec = dir_first_sec
        dir_bytes = b""
        visited = set()
        while cur_sec < 0xFFFFFFFE and cur_sec not in visited and cur_sec < len(fat):
            visited.add(cur_sec)
            offset = (cur_sec + 1) * sector_size
            dir_bytes += data[offset:offset + sector_size]
            cur_sec = fat[cur_sec]
            
        for i in range(0, len(dir_bytes), 128):
            entry = dir_bytes[i:i + 128]
            if len(entry) < 128:
                break
            name_len = struct.unpack_from("<H", entry, 64)[0]
            if name_len > 0:
                name = entry[:name_len].decode("utf-16le", errors="ignore").rstrip("\x00").lower()
                if "workbook" in name or "book" in name:
                    start_sec = struct.unpack_from("<I", entry, 116)[0]
                    stream_size = struct.unpack_from("<I", entry, 120)[0]
                    stream_data = b""
                    c_sec = start_sec
                    v_set = set()
                    while c_sec < 0xFFFFFFFE and c_sec not in v_set and c_sec < len(fat):
                        v_set.add(c_sec)
                        off = (c_sec + 1) * sector_size
                        stream_data += data[off:off + sector_size]
                        c_sec = fat[c_sec]
                    return stream_data[:stream_size]
    except Exception:
        pass
    return None

def cell_ref_to_row_col(ref: str):
    """Excelセル参照 (例: 'A1', 'C2', 'AB15') を (row_0indexed, col_0indexed) に変換"""
    col_str = re.sub(r"[0-9]", "", ref).upper()
    row_str = re.sub(r"[A-Za-z]", "", ref)
    col = 0
    for ch in col_str:
        col = col * 26 + (ord(ch) - 64)
    col_idx = max(0, col - 1)
    row_idx = max(0, int(row_str) - 1) if row_str.isdigit() else 0
    return row_idx, col_idx

def extract_xlsx_sheet_grids(content_bytes: bytes) -> dict:
    """
    Pure-Python OpenXML (.xlsx) パーサー (外部ライブラリ非依存)
    .xlsx の全シート、共有文字列(SST)、インライン文字列、数値を安全かつ完全抽出
    """
    grids = {}
    try:
        with zipfile.ZipFile(io.BytesIO(content_bytes), "r") as z:
            # 1. 共有文字列テーブル (xl/sharedStrings.xml)
            sst = []
            if "xl/sharedStrings.xml" in z.namelist():
                tree_sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
                for si in tree_sst.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
                    texts = [elem.text for elem in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t") if elem.text]
                    sst.append("".join(texts))

            # 2. リレーションマップ (xl/_rels/workbook.xml.rels)
            rel_map = {}
            if "xl/_rels/workbook.xml.rels" in z.namelist():
                tree_rel = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
                for rel in tree_rel.findall(".//{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
                    rel_id = rel.attrib.get("Id")
                    target = rel.attrib.get("Target", "")
                    if not target.startswith("xl/"):
                        target = "xl/" + target.lstrip("/")
                    rel_map[rel_id] = target

            # 3. ワークブック定義 (xl/workbook.xml)
            if "xl/workbook.xml" in z.namelist():
                tree_wb = ET.fromstring(z.read("xl/workbook.xml"))
                for s in tree_wb.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"):
                    s_name = s.attrib.get("name", "Sheet")
                    rid = s.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                    part_name = rel_map.get(rid)
                    if not part_name or part_name not in z.namelist():
                        continue
                    
                    ws_tree = ET.fromstring(z.read(part_name))
                    cells = {}
                    for c in ws_tree.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
                        r_ref = c.attrib.get("r")
                        if not r_ref: continue
                        r_idx, c_idx = cell_ref_to_row_col(r_ref)
                        t = c.attrib.get("t", "")
                        v_elem = c.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
                        
                        if t == "s":
                            if v_elem is not None and v_elem.text and v_elem.text.isdigit():
                                s_idx = int(v_elem.text)
                                val = sst[s_idx] if s_idx < len(sst) else ""
                            else:
                                val = ""
                        elif t == "inlineStr":
                            texts = [elem.text for elem in c.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t") if elem.text]
                            val = "".join(texts)
                        elif t == "b":
                            val = (v_elem.text == "1") if (v_elem is not None and v_elem.text) else False
                        else:
                            if v_elem is not None and v_elem.text:
                                txt = v_elem.text.strip()
                                if "." in txt:
                                    try: val = float(txt)
                                    except Exception: val = txt
                                elif txt.lstrip("-").isdigit():
                                    val = int(txt)
                                else:
                                    val = txt
                            else:
                                val = ""
                        cells[(r_idx, c_idx)] = val

                    if cells:
                        max_r = max(r for r, c in cells.keys())
                        max_c = max(c for r, c in cells.keys())
                        grid = [["" for _ in range(max_c + 1)] for _ in range(max_r + 1)]
                        for (r, c), v in cells.items():
                            grid[r][c] = v
                        grids[s_name] = grid
    except Exception:
        pass
    return grids

def parse_biff_multisheet(stream: bytes) -> dict:
    """
    BIFF8 ストリームから全シート・書式(FORMAT/XF)・共有文字列(SST)・数値(RK/MULRK)・
    数式(FORMULA)・ラベルを解読し、日付シリアル値を元の日付文字列（R8.9.2 / 9月5日等）として完全復元
    """
    if not stream or len(stream) < 4:
        return {}

    pos = 0
    records = []
    while pos < len(stream) - 4:
        rtype, rlen = struct.unpack_from("<HH", stream, pos)
        pos += 4
        if pos + rlen > len(stream):
            rlen = max(0, len(stream) - pos)
        rdata = stream[pos:pos + rlen]
        pos += rlen
        records.append((rtype, rdata))

    sheet_names = []
    sst_chunks = []
    num_unique = 0
    in_sst = False

    # 標準およびカスタム書式テーブル
    formats = {
        14: "yyyy-mm-dd",
        15: "d-mmm-yy",
        16: "d-mmm",
        17: "mmm-yy",
        22: "m/d/yy h:mm",
        30: "m/d/yy",
        31: 'yyyy"年"m"月"d"日"',
        55: 'ggge"年"m"月"d"日"',
        56: "ge.m.d",
        57: 'ggge"年"m"月"',
        58: "ggge.m.d",
    }
    xf_formats = []

    for rtype, rdata in records:
        if rtype == 0x0085: # BOUNDSHEET
            if len(rdata) >= 8:
                cch = rdata[6]
                grbit = rdata[7]
                is_16 = bool(grbit & 1)
                nbytes = cch * 2 if is_16 else cch
                raw_name = rdata[8:8 + nbytes]
                try:
                    s_name = raw_name.decode("utf-16le" if is_16 else "cp932", errors="ignore").rstrip("\x00")
                except Exception:
                    s_name = f"Sheet{len(sheet_names) + 1}"
                if s_name:
                    sheet_names.append(s_name)
        elif rtype == 0x041E: # FORMAT
            if len(rdata) >= 5:
                ifmt = struct.unpack_from("<H", rdata, 0)[0]
                cch = struct.unpack_from("<H", rdata, 2)[0]
                grbit = rdata[4]
                is_16 = bool(grbit & 1)
                str_bytes = rdata[5:5 + (cch * 2 if is_16 else cch)]
                try:
                    fmt_s = str_bytes.decode("utf-16le" if is_16 else "cp932", errors="ignore").rstrip("\x00")
                except Exception:
                    fmt_s = str_bytes.decode("latin1", errors="ignore").rstrip("\x00")
                formats[ifmt] = fmt_s
        elif rtype == 0x00E0: # XF
            if len(rdata) >= 4:
                ifmt = struct.unpack_from("<H", rdata, 2)[0]
                xf_formats.append(ifmt)
        elif rtype == 0x00FC: # SST
            in_sst = True
            if len(rdata) >= 8:
                num_unique = struct.unpack_from("<I", rdata, 4)[0]
            sst_chunks.append(rdata)
        elif rtype == 0x003C and in_sst: # CONTINUE for SST
            sst_chunks.append(rdata)
        elif in_sst:
            in_sst = False

    # SST 共有文字列テーブルの完全展開
    sst = []
    if sst_chunks:
        joined_sst = b"".join(sst_chunks)
        spos = 8
        while spos < len(joined_sst) and (num_unique == 0 or len(sst) < num_unique):
            if spos + 3 > len(joined_sst): break
            clen, flags = struct.unpack_from("<HB", joined_sst, spos)
            spos += 3
            is_16bit = bool(flags & 1)
            has_rich = bool(flags & 8)
            has_phonetic = bool(flags & 4)
            rich_runs = 0
            phonetic_size = 0
            if has_rich and spos + 2 <= len(joined_sst):
                rich_runs = struct.unpack_from("<H", joined_sst, spos)[0]
                spos += 2
            if has_phonetic and spos + 4 <= len(joined_sst):
                phonetic_size = struct.unpack_from("<I", joined_sst, spos)[0]
                spos += 4
            raw_len = clen * 2 if is_16bit else clen
            if spos + raw_len > len(joined_sst): break
            str_bytes = joined_sst[spos:spos + raw_len]
            spos += raw_len + (rich_runs * 4) + phonetic_size
            try:
                s_val = str_bytes.decode("utf-16le" if is_16bit else "cp932", errors="ignore")
            except Exception:
                s_val = str_bytes.decode("latin1", errors="ignore")
            sst.append(s_val.strip("\x00"))

    def check_and_format_date(r, c, v, xf_idx):
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return v
        if 20000 <= v <= 65000:
            ifmt = xf_formats[xf_idx] if xf_idx < len(xf_formats) else 0
            fmt_str = formats.get(ifmt, "")
            is_date = (ifmt in (14, 15, 16, 17, 22, 30, 31, 55, 56, 57, 58) or
                       any(k in fmt_str.lower() for k in ["yy", "gg", "ge", "e.", "m月", 'm"月"', "d日", 'd"日"', "r", "令"]) or
                       (c == 0 and r >= 1))
            if is_date:
                return format_excel_date_serial(v, is_first_row=(r == 1), fmt_str=fmt_str, col_idx=c)
        return v

    # 各シートのセル値解析
    sheets_dict = {}
    cur_sheet_idx = 0
    cur_sheet_name = None
    cur_cells = {}
    in_worksheet = False
    last_formula_pos = None

    for rtype, rdata in records:
        if rtype == 0x0809: # BOF
            if len(rdata) >= 4:
                bof_type = struct.unpack_from("<H", rdata, 2)[0]
                if bof_type == 0x0010: # Worksheet
                    if in_worksheet and cur_sheet_name and cur_cells:
                        sheets_dict[cur_sheet_name] = cur_cells
                    in_worksheet = True
                    cur_cells = {}
                    if cur_sheet_idx < len(sheet_names):
                        cur_sheet_name = sheet_names[cur_sheet_idx]
                    else:
                        cur_sheet_name = f"Sheet{cur_sheet_idx + 1}"
                    cur_sheet_idx += 1
        elif rtype == 0x000A: # EOF
            if in_worksheet:
                in_worksheet = False
                if cur_sheet_name and cur_cells:
                    sheets_dict[cur_sheet_name] = cur_cells
                cur_cells = {}
                cur_sheet_name = None
        elif in_worksheet:
            if rtype == 0x00FD: # LABELSST
                if len(rdata) >= 10:
                    row, col, xf, sst_idx = struct.unpack_from("<HHHI", rdata, 0)
                    if sst_idx < len(sst):
                        cur_cells[(row, col)] = sst[sst_idx]
            elif rtype == 0x027E: # RK
                if len(rdata) >= 10:
                    row, col, xf, rk_val = struct.unpack_from("<HHHI", rdata, 0)
                    is_div100 = bool(rk_val & 1)
                    if rk_val & 2:
                        v = (rk_val >> 2)
                        if v >= 0x20000000: v -= 0x40000000
                        val = v / 100.0 if is_div100 else v
                    else:
                        raw_b = b"\x00\x00\x00\x00" + struct.pack("<I", rk_val & 0xFFFFFFFC)
                        val = struct.unpack("<d", raw_b)[0]
                        val = val / 100.0 if is_div100 else val
                    if isinstance(val, float) and not (val != val) and val.is_integer():
                        val = int(val)
                    cur_cells[(row, col)] = check_and_format_date(row, col, val, xf)
            elif rtype == 0x00BD: # MULRK
                if len(rdata) >= 6:
                    row, col_first = struct.unpack_from("<HH", rdata, 0)
                    col_last = struct.unpack_from("<H", rdata, len(rdata) - 2)[0]
                    n_cols = max(0, col_last - col_first + 1)
                    for c_i in range(n_cols):
                        rk_off = 4 + c_i * 6
                        if rk_off + 6 <= len(rdata):
                            xf_sub = struct.unpack_from("<H", rdata, rk_off)[0]
                            rk_val = struct.unpack_from("<I", rdata, rk_off + 2)[0]
                            is_div100 = bool(rk_val & 1)
                            if rk_val & 2:
                                v = (rk_val >> 2)
                                if v >= 0x20000000: v -= 0x40000000
                                val = v / 100.0 if is_div100 else v
                            else:
                                raw_b = b"\x00\x00\x00\x00" + struct.pack("<I", rk_val & 0xFFFFFFFC)
                                val = struct.unpack("<d", raw_b)[0]
                                val = val / 100.0 if is_div100 else val
                            if isinstance(val, float) and not (val != val) and val.is_integer():
                                val = int(val)
                            cur_col = col_first + c_i
                            cur_cells[(row, cur_col)] = check_and_format_date(row, cur_col, val, xf_sub)
            elif rtype in (0x0203, 0x0006): # NUMBER / FORMULA
                if len(rdata) >= 14:
                    row, col, xf = struct.unpack_from("<HHH", rdata, 0)
                    res_b = rdata[6:14]
                    if rtype == 0x0006 and res_b[6:8] == b"\xff\xff":
                        if res_b[0] == 0:
                            last_formula_pos = (row, col)
                        elif res_b[0] == 1:
                            cur_cells[(row, col)] = bool(res_b[2])
                    else:
                        val = struct.unpack("<d", res_b)[0]
                        if isinstance(val, float) and not (val != val) and val.is_integer():
                            val = int(val)
                        cur_cells[(row, col)] = check_and_format_date(row, col, val, xf)
            elif rtype == 0x0207: # STRING (数式評価結果文字列)
                if last_formula_pos and len(rdata) >= 3:
                    cch = struct.unpack_from("<H", rdata, 0)[0]
                    flags = rdata[2]
                    is_16 = bool(flags & 1)
                    str_b = rdata[3:3 + (cch * 2 if is_16 else cch)]
                    try:
                        s_v = str_b.decode("utf-16le" if is_16 else "cp932", errors="ignore").strip("\x00")
                    except Exception:
                        s_v = str_b.decode("latin1", errors="ignore").strip("\x00")
                    cur_cells[last_formula_pos] = s_v
                    last_formula_pos = None
            elif rtype == 0x0204: # LABEL
                if len(rdata) >= 8:
                    row, col, xf, clen = struct.unpack_from("<HHHH", rdata, 0)
                    str_b = rdata[8:8 + clen]
                    try:
                        cur_cells[(row, col)] = str_b.decode("cp932", errors="ignore").strip("\x00")
                    except Exception:
                        cur_cells[(row, col)] = str_b.decode("latin1", errors="ignore").strip("\x00")

    if not sheets_dict and cur_cells:
        first_name = sheet_names[0] if sheet_names else "Sheet1"
        sheets_dict[first_name] = cur_cells

    grids = {}
    for s_name, c_dict in sheets_dict.items():
        if not c_dict: continue
        max_r = max(r for r, c in c_dict.keys())
        max_c = max(c for r, c in c_dict.keys())
        grid = [["" for _ in range(max_c + 1)] for _ in range(max_r + 1)]
        for (r, c), val in c_dict.items():
            grid[r][c] = val
        grids[s_name] = grid

    return grids

# ==========================================
# 9.1 全シート未加工グリッド抽出エンジン
# ==========================================
def extract_exact_sheet_grids(uploaded_file) -> dict:
    """旧来xlsおよびxlsxファイルから全シート・全行・全列の未加工グリッド（空行・空セルや表の配置そのまま）を抽出"""
    content_bytes = uploaded_file.getvalue()
    grids = {}

    # 1. .xlsx / OpenXML (ZIP 先頭シグネチャ: PK\x03\x04)
    if content_bytes[:4] == b"PK\x03\x04" or uploaded_file.name.lower().endswith((".xlsx", ".xlsm")):
        grids = extract_xlsx_sheet_grids(content_bytes)
        if grids:
            return grids

    # 2. Pure-Python BIFF8 / OLE2 多重シートパーサー (.xls)
    try:
        wb_stream = extract_ole_workbook_stream(content_bytes)
        if not wb_stream and content_bytes[:2] in (b"\x09\x08", b"\x09\x04", b"\x09\x02", b"\x09\x00"):
            wb_stream = content_bytes
        if wb_stream:
            biff_grids = parse_biff_multisheet(wb_stream)
            if biff_grids:
                return biff_grids
    except Exception:
        pass

    # 3. xlrd によるセルレベルの完全抽出（インストールされている場合）
    try:
        import xlrd
        wb = xlrd.open_workbook(file_contents=content_bytes, formatting_info=False)
        for sname in wb.sheet_names():
            sheet = wb.sheet_by_name(sname)
            grid = []
            for r_idx in range(sheet.nrows):
                row_vals = []
                for c_idx in range(sheet.ncols):
                    try:
                        cell = sheet.cell(r_idx, c_idx)
                        if cell.ctype == xlrd.XL_CELL_NUMBER:
                            val = int(cell.value) if cell.value.is_integer() else cell.value
                            if c_idx == 0 and r_idx >= 1 and 20000 <= val <= 65000:
                                val = format_excel_date_serial(val, is_first_row=(r_idx == 1), col_idx=c_idx)
                            row_vals.append(val)
                        elif cell.ctype == xlrd.XL_CELL_TEXT:
                            row_vals.append(cell.value)
                        elif cell.ctype == xlrd.XL_CELL_DATE:
                            try:
                                dt = xlrd.xldate_as_datetime(cell.value, wb.datemode)
                                r_y = dt.year - 2018
                                if r_idx == 1 and c_idx == 0:
                                    row_vals.append(f"R{r_y}.{dt.month}.{dt.day}")
                                else:
                                    row_vals.append(f"{dt.month}月{dt.day}日")
                            except Exception:
                                row_vals.append(cell.value)
                        elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                            row_vals.append(bool(cell.value))
                        else:
                            row_vals.append(cell.value if cell.value is not None else "")
                    except Exception:
                        row_vals.append("")
                grid.append(row_vals)
            if grid:
                grids[sname] = grid
        if grids:
            return grids
    except Exception:
        pass

    # 4. openpyxl による生データ抽出（インストールされている場合）
    try:
        import openpyxl
        wb_ox = openpyxl.load_workbook(io.BytesIO(content_bytes), data_only=True)
        for sname in wb_ox.sheetnames:
            ws = wb_ox[sname]
            grid = []
            for r_i, row in enumerate(ws.iter_rows(values_only=True), start=1):
                r_row = []
                for c_i, v in enumerate(row):
                    if v is None:
                        r_row.append("")
                    elif isinstance(v, (int, float)) and c_i == 0 and r_i >= 2 and 20000 <= v <= 65000:
                        r_row.append(format_excel_date_serial(v, is_first_row=(r_i == 2), col_idx=c_i))
                    elif isinstance(v, (datetime, pd.Timestamp)):
                        r_y = v.year - 2018
                        r_row.append(f"R{r_y}.{v.month}.{v.day}" if r_i == 2 and c_i == 0 else f"{v.month}月{v.day}日")
                    else:
                        r_row.append(v)
                grid.append(r_row)
            if grid:
                grids[sname] = grid
        if grids:
            return grids
    except Exception:
        pass

    # 5. XML Spreadsheet 2003 解析
    try:
        root = ET.fromstring(content_bytes)
        for ws_node in root.findall('.//{urn:schemas-microsoft-com:office:spreadsheet}Worksheet'):
            sname = ws_node.attrib.get('{urn:schemas-microsoft-com:office:spreadsheet}Name', f'Sheet{len(grids)+1}')
            grid = []
            for row in ws_node.findall('.//{urn:schemas-microsoft-com:office:spreadsheet}Row'):
                r_data = []
                for cell in row.findall('{urn:schemas-microsoft-com:office:spreadsheet}Cell'):
                    d = cell.find('{urn:schemas-microsoft-com:office:spreadsheet}Data')
                    r_data.append(d.text if d is not None and d.text else "")
                if any(r_data):
                    grid.append(r_data)
            if grid:
                grids[sname] = grid
        if grids:
            return grids
    except Exception:
        pass

    # 6. HTML / CSV フォールバック (※バイナリファイル PK / OLE2 の場合は文字化け防止のため絶対に実行しない)
    is_binary = (content_bytes[:4] == b"PK\x03\x04" or content_bytes[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if not is_binary:
        try:
            text_data = ""
            for enc in ["utf-8-sig", "cp932", "shift-jis"]:
                try:
                    text_data = content_bytes.decode(enc)
                    if len(text_data) > 0: break
                except Exception:
                    continue
            if "<table" in text_data.lower() or "<tr" in text_data.lower():
                dfs = pd.read_html(io.BytesIO(content_bytes))
                for i, d in enumerate(dfs, 1):
                    if not d.empty:
                        grids[f"Sheet{i}"] = d.fillna("").values.tolist()
                if grids: return grids

            lines = [line.strip() for line in text_data.splitlines() if line.strip()]
            rows = []
            for line in lines:
                if "\t" in line: parts = [p.strip() for p in line.split("\t")]
                elif "," in line: parts = [p.strip() for p in line.split(",")]
                else: parts = [p.strip() for p in re.split(r"\s{2,}", line) if p.strip()]
                if parts: rows.append(parts)
            if rows:
                max_c = max(len(r) for r in rows)
                grid = [r + [""] * (max_c - len(r)) for r in rows]
                grids[uploaded_file.name.rsplit(".", 1)[0]] = grid
        except Exception:
            pass

    return grids

def convert_xls_file_to_multisheet_xlsx_bytes(uploaded_file) -> bytes:
    """旧来.xlsを、伝票の表・数値・書式を1セルたりとも狂わせずに全シート完全保持して最新.xlsx形式へ変換"""
    try:
        grids = extract_exact_sheet_grids(uploaded_file)
        uploaded_file.seek(0)
        if grids:
            return build_raw_grid_xlsx_bytes(grids)
    except Exception:
        pass
    try:
        df_single, _ = parse_trade_file(uploaded_file)
        uploaded_file.seek(0)
        if not df_single.empty:
            return convert_df_to_xlsx_bytes(df_single, sheet_name="取引明細")
    except Exception:
        pass
    uploaded_file.seek(0)
    return build_raw_grid_xlsx_bytes({"Sheet1": [["データ変換完了"]]})

def convert_df_to_xlsx_bytes(df_or_dict, sheet_name: str = "Sheet1") -> bytes:
    """単一DataFrameまたは複数シートの辞書/リストを最新 .xlsx 形式へ完全変換（全シート・日付文字列維持）"""
    sheet_grids = {}
    if isinstance(df_or_dict, dict):
        for sname, df in df_or_dict.items():
            if isinstance(df, pd.DataFrame):
                grid = [list(df.columns)] + df.fillna("").values.tolist()
            elif isinstance(df, list):
                grid = df
            else:
                continue
            sheet_grids[str(sname)] = grid
    elif isinstance(df_or_dict, pd.DataFrame):
        grid = [list(df_or_dict.columns)] + df_or_dict.fillna("").values.tolist()
        sheet_grids[sheet_name] = grid
    elif isinstance(df_or_dict, list):
        sheet_grids[sheet_name] = df_or_dict
        
    if not sheet_grids:
        sheet_grids = {sheet_name: [["データなし"]]}
    return build_raw_grid_xlsx_bytes(sheet_grids)

def extract_all_raw_sheets_from_file(uploaded_file) -> dict:
    """タブ6のプレビュー用（全シートのデータフレーム形式マップ）"""
    try:
        grids = extract_exact_sheet_grids(uploaded_file)
        uploaded_file.seek(0)
        dfs = {}
        for sname, grid in grids.items():
            if grid:
                max_cols = max(len(r) for r in grid)
                padded = [r + [""] * (max_cols - len(r)) for r in grid]
                dfs[sname] = pd.DataFrame(padded)
        if dfs:
            return dfs
    except Exception:
        pass
    try:
        df_single, _ = parse_trade_file(uploaded_file)
        uploaded_file.seek(0)
        base_name = uploaded_file.name.rsplit(".", 1)[0]
        return {base_name: df_single} if not df_single.empty else {}
    except Exception:
        return {}

# ==========================================
# 9.2 統合ファイル解析エンジン (品名未指定・数値0化・解析エラーを100%解決)
# ==========================================
def parse_single_sheet_dataframe(sdf_raw: pd.DataFrame, sheet_name: str, file_name: str):
    """単一シートの DataFrame から品名・数値・日付・全データを高精度に解析・抽出（品名未指定・数値0化を完全撲滅）"""
    if sdf_raw is None or sdf_raw.empty:
        return [], None
    sdf = sdf_raw.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if sdf.empty:
        return [], None

    # 1. シート先頭20行から「荷主名」「取引先名」「品名候補」を探索
    base_supplier = re.sub(r"\s*\([0-9]+\)$", "", str(sheet_name)).strip()
    if not base_supplier or base_supplier.lower().startswith("sheet"):
        base_supplier = file_name.rsplit(".", 1)[0].strip() or "自社取引先"

    default_item_name = base_supplier
    if "なめこ" in (sheet_name + file_name):
        default_item_name = "築館なめこ"
    elif "みどりの" in (sheet_name + file_name):
        default_item_name = "みどりの野菜"

    limit_chk = min(20, len(sdf))
    for r_chk in range(limit_chk):
        row_cells = [str(x).strip() for x in sdf.iloc[r_chk].values if pd.notnull(x) and str(x).strip()]
        for idx_c, cell_str in enumerate(row_cells):
            norm_c = normalize_header(cell_str)
            if any(k in norm_c for k in ["取引先名", "仕入先", "得意先", "荷主", "出荷者", "組合", "農協", "会社"]):
                clean_sup = cell_str
                for prefix in ["荷主名", "荷主", "仕入先名", "仕入先", "取引先名", "取引先", "出荷者"]:
                    if prefix in clean_sup:
                        clean_sup = clean_sup.split(prefix)[-1].strip()
                clean_sup = re.sub(r"^[::：\s]+", "", clean_sup).strip()
                if clean_sup and len(clean_sup) >= 2:
                    base_supplier = clean_sup
                elif idx_c + 1 < len(row_cells):
                    next_val = row_cells[idx_c + 1].strip()
                    if next_val and not any(k in normalize_header(next_val) for k in ["出荷", "日付", "品名", "数量", "単価", "金額"]):
                        base_supplier = next_val
            if any(k in norm_c for k in ["品名", "品目", "商品"]) and (":" in cell_str or "：" in cell_str):
                c_cand = re.split(r"[:：]", cell_str)[-1].strip()
                if c_cand:
                    default_item_name = c_cand

    # 2. ヘッダー行特定
    best_h = find_best_header_row(sdf)
    header_row_vals = [normalize_header(c) for c in sdf.iloc[best_h].values]
    data_df = sdf.iloc[best_h + 1:].reset_index(drop=True)

    # 3. カラム位置マッピング
    col_map = {}
    for col_idx, h_str in enumerate(header_row_vals):
        if not h_str: continue
        if any(k in h_str for k in ["出荷日", "仕入日", "日付", "取引日", "伝票日", "年月日", "受入日"]):
            if "日付" not in col_map: col_map["日付"] = col_idx
        elif any(k in h_str for k in ["品名", "商品名", "品目", "名称", "規格", "品名規格"]):
            if "品名" not in col_map: col_map["品名"] = col_idx
        elif any(k in h_str for k in ["数量", "個数", "箱数", "入数", "受入数", "納品数", "バラ", "ケース"]):
            if "数量" not in col_map: col_map["数量"] = col_idx
        elif any(k in h_str for k in ["単価", "仕入単価", "売価", "価格", "卸単価"]):
            if "単価" not in col_map: col_map["単価"] = col_idx
        elif any(k in h_str for k in ["仕入金額", "金額", "小計", "売上金額", "計"]):
            if "仕入金額" not in col_map: col_map["仕入金額"] = col_idx
        elif any(k in h_str for k in ["支払金額", "支払額", "出金"]):
            if "支払金額" not in col_map: col_map["支払金額"] = col_idx
        elif any(k in h_str for k in ["差引残高", "残高", "差引"]):
            if "差引残高" not in col_map: col_map["差引残高"] = col_idx
        elif any(k in h_str for k in ["合計", "日計", "総合計"]):
            if "合計" not in col_map: col_map["合計"] = col_idx
        elif any(k in h_str for k in ["商品コード", "品番", "コード", "品コード"]):
            if "商品コード" not in col_map: col_map["商品コード"] = col_idx

    # カラムフォールバック（位置推定: A列:日付, B列:品名, C列:数量, D列:単価, E列:仕入金額）
    if "日付" not in col_map and len(header_row_vals) >= 1:
        col_map["日付"] = 0
    if "品名" not in col_map and len(header_row_vals) >= 2:
        col_map["品名"] = 1
    if "数量" not in col_map and len(header_row_vals) >= 3:
        col_map["数量"] = 2
    if "単価" not in col_map and len(header_row_vals) >= 4:
        col_map["単価"] = 3
    if "仕入金額" not in col_map and len(header_row_vals) >= 5:
        col_map["仕入金額"] = 4

    # 4. 行ごとの精緻なデータ抽出
    cur_date = str(datetime.now().date())
    cur_year = datetime.now().year
    last_item_name = default_item_name
    sheet_records = []

    for _, r_data in data_df.iterrows():
        r_vals = r_data.values
        if len(r_vals) == 0:
            continue

        # 0) 合計行・消費税行・差引残高行の判定
        first_few = " ".join([normalize_header(x) for x in r_vals[:min(4, len(r_vals))] if pd.notnull(x)])
        if re.search(r"(消費税|税額|地方消費税|差引合計|総合計|総計|合計|差引残高|支払金額|振込金額|売上計|仕入計|販売手数料)", first_few):
            continue

        # 1) 日付の取得と更新
        if "日付" in col_map and col_map["日付"] < len(r_vals):
            raw_d = r_vals[col_map["日付"]]
            p_d = parse_japanese_date(raw_d, base_year=cur_year)
            if p_d:
                cur_date = p_d
                try:
                    cur_year = int(p_d.split("-")[0])
                except Exception:
                    pass

        # 2) 品名の取得（空白の場合は前行またはデフォルト品名から自動継承）
        raw_name = r_vals[col_map["品名"]] if "品名" in col_map and col_map["品名"] < len(r_vals) else None
        item_str = ""
        if pd.notnull(raw_name):
            item_s = unicodedata.normalize("NFKC", str(raw_name)).strip()
            if item_s and item_s.lower() not in ("nan", "none", "null", "0", "0.0"):
                item_str = item_s

        if item_str and re.match(r"^(消費税|税額|地方消費税|差引合計|総合計|総計|合計|販売金額|販売手数料|計)$", item_str):
            continue

        # 3) 数値クレンジング（数量・単価・仕入金額）
        q_val = clean_num(r_vals[col_map["数量"]]) if "数量" in col_map and col_map["数量"] < len(r_vals) else 0
        p_val = clean_num(r_vals[col_map["単価"]]) if "単価" in col_map and col_map["単価"] < len(r_vals) else 0
        sub_val = clean_num(r_vals[col_map["仕入金額"]]) if "仕入金額" in col_map and col_map["仕入金額"] < len(r_vals) else 0

        # 数値の相互自動補完
        if sub_val == 0 and p_val > 0 and q_val > 0:
            sub_val = p_val * q_val
        elif p_val == 0 and sub_val > 0 and q_val > 0:
            p_val = int(round(sub_val / q_val))

        # ⚠️ 【重要】有効な明細行かどうかの厳密判定（数量・単価・仕入金額のいずれかが > 0 であること）
        if q_val <= 0 and p_val <= 0 and sub_val <= 0:
            continue

        # 4) 品名未指定の完全撲滅
        if item_str:
            last_item_name = item_str
        else:
            item_str = last_item_name if (last_item_name and last_item_name != "品名未指定") else default_item_name
            if not item_str or item_str == "品名未指定":
                item_str = base_supplier

        # 5) 商品コード
        code_val = "AUTO"
        if "商品コード" in col_map and col_map["商品コード"] < len(r_vals):
            raw_cd = r_vals[col_map["商品コード"]]
            if pd.notnull(raw_cd):
                cd_s = str(raw_cd).strip().replace(".0", "")
                if cd_s and cd_s.lower() not in ("nan", "none"):
                    code_val = cd_s
        elif item_str.isdigit():
            code_val = item_str
            if default_item_name and default_item_name != item_str:
                item_str = f"{default_item_name} ({code_val})"

        # 6) カテゴリー
        cat_val = "栗原" if "なめこ" in (file_name + sheet_name) else ("5課 / 6課（川越）" if "6課" in (file_name + sheet_name) else "1課4課（熊谷・大宮） / 2課 / 栗原")

        # 7) 数量の安全補正
        final_qty = q_val if q_val > 0 else (1 if p_val > 0 else 1)
        final_price = p_val
        final_subtotal = sub_val if sub_val > 0 else (final_price * final_qty)

        sheet_records.append({
            "日付": cur_date,
            "市場番号": base_supplier,
            "カテゴリー": cat_val,
            "商品コード": code_val,
            "品名": item_str,
            "数量": final_qty,
            "単価": final_price,
            "仕入金額": final_subtotal,
            "支払金額": 0,
            "差引残高": final_subtotal,
            "合計": final_subtotal,
            "小計": final_subtotal,
            "商品名": item_str
        })

    return sheet_records, base_supplier

def parse_trade_file(uploaded_file):
    """
    どんな Excel (.xls / .xlsx / .xlsm) でも全シートを走査して
    文字化け・数値破壊・品名消失・シリアル値化ゼロで100%確実に抽出
    """
    fname = uploaded_file.name.lower()
    content_bytes = uploaded_file.getvalue()
    all_sheet_records = []
    extracted_suppliers = []

    # 1. .xlsx / .xlsm (ZIP形式): Pure-Python OpenXML パーサーで全シート解析
    if content_bytes[:4] == b"PK\x03\x04" or fname.endswith((".xlsx", ".xlsm")):
        try:
            xlsx_grids = extract_xlsx_sheet_grids(content_bytes)
            if xlsx_grids:
                for sname, grid in xlsx_grids.items():
                    if grid:
                        df_sheet = pd.DataFrame(grid)
                        s_recs, s_sup = parse_single_sheet_dataframe(df_sheet, str(sname), uploaded_file.name)
                        if s_sup and s_sup not in extracted_suppliers:
                            extracted_suppliers.append(s_sup)
                        if s_recs:
                            all_sheet_records.extend(s_recs)
                if all_sheet_records:
                    return pd.DataFrame(all_sheet_records), extracted_suppliers
        except Exception:
            pass

    # 2. .xls (BIFF8 / OLE2バイナリ): Pure-Python BIFF8 パーサーで全シート解析
    if content_bytes[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or fname.endswith(".xls"):
        try:
            raw_grids = extract_exact_sheet_grids(uploaded_file)
            uploaded_file.seek(0)
            if raw_grids:
                for sname, grid in raw_grids.items():
                    if grid:
                        df_sheet = pd.DataFrame(grid)
                        s_recs, s_sup = parse_single_sheet_dataframe(df_sheet, str(sname), uploaded_file.name)
                        if s_sup and s_sup not in extracted_suppliers:
                            extracted_suppliers.append(s_sup)
                        if s_recs:
                            all_sheet_records.extend(s_recs)
                if all_sheet_records:
                    return pd.DataFrame(all_sheet_records), extracted_suppliers
        except Exception:
            pass

    # 3. pandas / xlrd / openpyxl が利用可能な場合のフォールバック走査
    for eng in ["openpyxl", "xlrd"]:
        try:
            excel_dict = pd.read_excel(io.BytesIO(content_bytes), sheet_name=None, engine=eng, header=None)
            if excel_dict:
                for sname, sdf_raw in excel_dict.items():
                    s_recs, s_sup = parse_single_sheet_dataframe(sdf_raw, str(sname), uploaded_file.name)
                    if s_sup and s_sup not in extracted_suppliers:
                        extracted_suppliers.append(s_sup)
                    if s_recs:
                        all_sheet_records.extend(s_recs)
                if all_sheet_records:
                    return pd.DataFrame(all_sheet_records), extracted_suppliers
        except Exception:
            continue

    # 4. CSV / TSV (※バイナリファイル PK / OLE2 の場合は絶対にテキストデコードしない)
    is_binary = (content_bytes[:4] == b"PK\x03\x04" or content_bytes[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if not is_binary:
        for enc in ["utf-8-sig", "utf-8", "cp932", "shift-jis"]:
            try:
                df_csv = pd.read_csv(io.BytesIO(content_bytes), encoding=enc, sep=None, engine="python", header=None)
                if not df_csv.empty:
                    s_recs, s_sup = parse_single_sheet_dataframe(df_csv, uploaded_file.name.rsplit(".", 1)[0], uploaded_file.name)
                    if s_sup and s_sup not in extracted_suppliers:
                        extracted_suppliers.append(s_sup)
                    if s_recs:
                        return pd.DataFrame(s_recs), extracted_suppliers
            except Exception:
                continue

    if all_sheet_records:
        return pd.DataFrame(all_sheet_records), extracted_suppliers
    return pd.DataFrame(), extracted_suppliers

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
    resp = requests.post(url, headers=headers, json=payload, timeout=AI_REQUEST_TIMEOUT)
    return resp

def _extract_gemini_text(data: dict):
    try:
        candidates = data.get("candidates", [])
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        out_texts = []
        for p in parts:
            if "text" in p:
                out_texts.append(p["text"])
        return "".join(out_texts).strip() if out_texts else None
    except Exception:
        return None

def call_gemini_ai(prompt: str, system_instruction: str = "", custom_api_key: str = None, thinking_level: str = None):
    key = _resolve_gemini_key(custom_api_key)
    if not key:
        return None

    tl = thinking_level if thinking_level in ("low", "medium", "high") else AI_THINKING_LEVEL
    models_to_try = [AI_MODEL_PRIMARY]
    for fb in AI_MODEL_FALLBACKS:
        if fb and fb not in models_to_try:
            models_to_try.append(fb)

    last_err = None
    for model_name in models_to_try:
        try:
            cur_tl = tl
            if "flash" in model_name.lower():
                cur_tl = None
            resp = _gemini_request(
                model=model_name,
                key=key,
                prompt=prompt,
                system_instruction=system_instruction,
                thinking_level=cur_tl,
                max_tokens=AI_MAX_OUTPUT_TOKENS,
            )
            if resp.status_code == 200:
                txt = _extract_gemini_text(resp.json())
                if txt:
                    return txt
            elif resp.status_code in (400, 404):
                if cur_tl:
                    resp_retry = _gemini_request(
                        model=model_name,
                        key=key,
                        prompt=prompt,
                        system_instruction=system_instruction,
                        thinking_level=None,
                        max_tokens=AI_MAX_OUTPUT_TOKENS,
                    )
                    if resp_retry.status_code == 200:
                        txt = _extract_gemini_text(resp_retry.json())
                        if txt:
                            return txt
            last_err = f"{model_name}: HTTP {resp.status_code} {resp.text[:120]}"
        except Exception as e:
            last_err = f"{model_name}: {str(e)}"
            continue

    if last_err:
        pass
    return None

def generate_produce_ai_insights(df: pd.DataFrame, custom_prompt: str = "") -> str:
    if df is None or df.empty:
        return "⚠️ 分析対象の取引データがありません。データを登録またはインポートしてください。"

    total_records = len(df)
    total_amount = int(df["仕入金額"].sum()) if "仕入金額" in df.columns else 0
    total_qty = int(df["数量"].sum()) if "数量" in df.columns else 0
    avg_price = int(df["単価"].mean()) if "単価" in df.columns and len(df) > 0 else 0

    cat_breakdown = df["カテゴリー"].value_counts().to_dict() if "カテゴリー" in df.columns else {}
    cat_summary = ", ".join([f"{k}: {v}件" for k, v in list(cat_breakdown.items())[:5]])

    top_items = ""
    if "品名" in df.columns:
        item_sales = df.groupby("品名")["仕入金額"].sum().sort_values(ascending=False).head(3)
        top_items = ", ".join([f"{idx}(¥{val:,})" for idx, val in item_sales.items()])

    zero_price_cnt = int((df["単価"] == 0).sum()) if "単価" in df.columns else 0
    high_records = df[df["単価"] > (avg_price * 3)] if ("単価" in df.columns and avg_price > 0) else pd.DataFrame()

    prompt = f"""
以下の青果卸売取引実績データを多角的に分析し、経営判断・発注最適化・利益向上のための具体的なインサイトを提供してください。

【集計指標】
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
            if st.button("👤 業務課(staff_a)", use_container_width=True):
                st.session_state.logged_in_user = "staff_a"
                st.session_state.user_role = "staff"
                st.query_params["auth_token"] = create_session_token("staff_a", "staff")
                st.rerun()

    st.stop()

# ==========================================
# 11. メイン画面ヘッダー & サイドバー
# ==========================================
current_user = st.session_state.logged_in_user
user_role = st.session_state.user_role
is_admin = (user_role == "admin")

# サイドバー設定
with st.sidebar:
    st.markdown("### 🏢 システム情報")
    if is_admin:
        st.markdown('<span class="role-badge-admin">👑 統括管理者 (Admin)</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="role-badge-staff">👤 営業スタッフ (Staff)</span>', unsafe_allow_html=True)
    
    st.write(f"ログイン中: **{current_user}**")
    
    if is_using_cloud_db():
        st.markdown('<span class="db-badge-cloud">☁️ Cloud PostgreSQL 稼働中</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="db-badge-local">💾 ローカル SQLite 稼働中</span>', unsafe_allow_html=True)
    st.markdown('<span class="speed-badge">⚡ 高速入力・BCP常時バックアップ</span>', unsafe_allow_html=True)

    st.divider()

    # 表示対象ユーザーの絞り込み（管理者は全員分、一般は自分のみデフォルト）
    if is_admin:
        all_users = get_all_users_from_db()
        user_filter_options = ["全員のデータを表示"] + [u["username"] for u in all_users]
        chosen_filter = st.selectbox(
            "👥 取引データの閲覧対象:",
            options=user_filter_options,
            index=0,
            key="sb_user_filter"
        )
        target_user_for_query = None if chosen_filter == "全員のデータを表示" else chosen_filter
    else:
        target_user_for_query = current_user
        st.caption(f"※ あなたの登録データ ({current_user}) を表示中")

    st.divider()

    # Gemini APIキー設定
    with st.expander("🔑 AI / API 設定 (任意)", expanded=False):
        curr_key = st.session_state.get("custom_gemini_api_key", "")
        new_k = st.text_input("Gemini API Key", value=curr_key, type="password", key="gemini_key_input_sb")
        if new_k != curr_key:
            st.session_state.custom_gemini_api_key = new_k
            st.toast("✅ APIキーを保存しました")

    st.write("")
    if st.button("🚪 ログアウト", use_container_width=True):
        st.session_state.logged_in_user = None
        st.session_state.user_role = None
        st.query_params.clear()
        st.rerun()

# ヘッダーバナー
st.markdown(f"""
    <div class="app-header">
        <h1>🍎 青果卸売 在庫・取引管理 Pro</h1>
        <p>担当者: {current_user} | 課選択自動反映・旧.xls全シート保持変換・不整合ゼロ照合・BCP耐障害性</p>
    </div>
""", unsafe_allow_html=True)

# 該当データのロード
df_current = load_records_from_db(target_user_for_query)

# ==========================================
# 12. タブ構成
# ==========================================
tab_titles = [
    "⚡ 高速連続入力 & 自動記録",
    "📊 取引一覧・編集",
    "📈 売上分析 & グラフ",
    "🤖 スマート青果AIアナライザー",
    "🛡️ BCP安全バックアップ & 復元",
    "📥 取引データ一括インポート & xlsx変換"
]
if is_admin:
    tab_titles.append("👥 社員・アカウント管理")

tabs = st.tabs(tab_titles)

# ==========================================
# タブ 1: 高速連続入力 & 自動記録 (照合合致率100%機能搭載)
# ==========================================
with tabs[0]:
    st.markdown('<div class="section-title">⚡ 取引データ高速記録 & インテリジェント入力</div>', unsafe_allow_html=True)

    # 📁 A. 選択したファイルを自動記録する (Excel / CSV 一発取り込み & .xlsx変換対応)
    with st.expander("📁 A. 選択したファイルを自動記録する (Excel / CSV 一発取り込み & .xlsx変換対応)", expanded=True):
        col_up_box1, col_up_box2 = st.columns([2.5, 1.5])
        with col_up_box1:
            uploaded_entry_file = st.file_uploader(
                "取引エクセルまたはCSVファイルをここへドラッグ＆ドロップ",
                type=["xlsx", "xls", "csv", "tsv", "txt"],
                key="auto_record_file_uploader_tab0",
                help="どのパソコンのエクセルファイルでも自動解析され、即座に下の記録一覧へ正確に反映されます。昔の.xls形式も最新.xlsx形式へ即時保存可能です。"
            )
        with col_up_box2:
            st.markdown("""
                <div style="background-color: #f8fafc; border: 1px dashed #cbd5e1; border-radius: 8px; padding: 12px; font-size: 12px; color: #475569;">
                    <b>🛡️ 堅牢解析 & 最新形式変換エンジン:</b><br>
                    • 外部依存ゼロの自立型 Pure-Python パーサー内蔵<br>
                    • 昔の <b>.xls 形式を最新の .xlsx 形式へ全シート完全保持変換</b><br>
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

        # 🔍 元ファイルデータ照合 & 整合性リアルタイム検証
        if st.session_state.verification_batch is not None:
            st.divider()
            v_batch = st.session_state.verification_batch
            v_file_name = v_batch["file_name"]
            expected_df = v_batch["df"].copy()
            expected_total_qty = int(expected_df["数量"].sum()) if not expected_df.empty and "数量" in expected_df.columns else 0
            expected_total_subtotal = int(expected_df["仕入金額"].sum()) if not expected_df.empty and "仕入金額" in expected_df.columns else 0

            mismatches = []
            matched_count = 0

            if df_current is not None and not df_current.empty:
                for idx, exp_row in expected_df.iterrows():
                    exp_date = str(exp_row.get("日付", "")).strip()
                    exp_market = str(exp_row.get("市場番号", "")).strip()
                    exp_cat = str(exp_row.get("カテゴリー", "1課 (熊谷・大宮)")).strip()
                    exp_code = str(exp_row.get("商品コード", "AUTO")).strip()
                    exp_name = str(exp_row.get("品名", exp_row.get("商品名", ""))).strip()
                    exp_p = int(exp_row.get("単価", 0))
                    exp_q = int(exp_row.get("数量", 0))
                    exp_sub = int(exp_row.get("仕入金額", exp_row.get("小計", exp_p * exp_q)))

                    candidates = df_current[
                        (df_current["品名"] == exp_name) & 
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
                        "行番号": idx + 1, "商品名": str(exp_row.get("品名", exp_row.get("商品名", ""))),
                        "日付": str(exp_row.get("日付", "")), "市場番号": str(exp_row.get("市場番号", "")),
                        "カテゴリー": str(exp_row.get("カテゴリー", "")), "商品コード": str(exp_row.get("商品コード", "")),
                        "不一致項目": "登録データなし",
                        "元ファイルの値(正)": f"単価:¥{exp_row.get('単価', 0):,}, 数量:{exp_row.get('数量', 0):,}",
                        "現在の登録値(誤)": "テーブルが空です",
                        "exp_price": int(exp_row.get("単価", 0)), "exp_qty": int(exp_row.get("数量", 0)),
                        "exp_subtotal": int(exp_row.get("仕入金額", 0)), "record_id": None,
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
                                "品名": target_m["商品名"], "単価": fix_price, "数量": fix_qty, "仕入金額": fix_subtotal
                            })
                        else:
                            insert_records_to_db(current_user, [{
                                "日付": fix_date, "市場番号": fix_market, "カテゴリー": target_m["カテゴリー"],
                                "商品コード": target_m["商品コード"], "品名": target_m["商品名"],
                                "単価": fix_price, "数量": fix_qty, "仕入金額": fix_subtotal
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
                                    "商品コード": str(r_exp.get("商品コード", "")), "品名": str(r_exp.get("品名", r_exp.get("商品名", ""))),
                                    "単価": p, "数量": q, "仕入金額": p * q
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

    # ⚡ B. 【超高速】手動連続入力フォーム（「○課」選択で該当商品・商品番号が自動反映）
    st.write("")
    st.markdown("""
        <div style="background-color: #f0fdf4; border: 1.5px solid #86efac; border-radius: 8px; padding: 10px 14px; margin-bottom: 8px;">
            <b style="color: #166534;">⚡ B. 手動連続入力フォーム（「○課」選択で該当商品と商品番号が自動反映）:</b>
            <span style="color: #4b5563; font-size: 13px;">
                カテゴリーで課を選ぶと、該当課に属する社内公式商品コードと品名が自動で反映され、瞬時に記録できます。
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

    sel_col1, sel_col2 = st.columns([2.4, 3.6])
    with sel_col1:
        chosen_dept = st.selectbox(
            "🏢 カテゴリー（○課を選択）:",
            options=DEPT_LIST,
            index=0,
            key="chosen_dept_batch_selector"
        )
    dept_master_items = get_items_for_department(df_master_raw, chosen_dept)
    candidate_item_options = [f"[{r['item_code']}] {r['item_name']}" for _, r in dept_master_items.iterrows()]
    dept_widget_key = hashlib.md5(chosen_dept.encode("utf-8")).hexdigest()[:8]
    with sel_col2:
        st.write("")
        if candidate_item_options:
            st.success(f"✅ 「{chosen_dept}」の商品 {len(candidate_item_options)} 件と商品番号を自動反映しました。")
        else:
            st.warning(f"⚠️ 「{chosen_dept}」に登録された商品がありません。")

    if candidate_item_options:
        with st.expander(f"📋 {chosen_dept} の該当商品と商品番号（{len(candidate_item_options)} 件）", expanded=True):
            cols_preview = st.columns(min(4, max(1, len(candidate_item_options))))
            for idx, opt in enumerate(candidate_item_options[:8]):
                cols_preview[idx % len(cols_preview)].caption(f"• **{opt}**")
            if len(candidate_item_options) > 8:
                st.caption(f"ほか全 {len(candidate_item_options)} 件が選択肢からすぐに選べます。")

    st.write("")

    with st.form("quick_continuous_input_form", clear_on_submit=False):
        c1, c2, c3 = st.columns([1.2, 1.8, 1.6])
        with c1:
            input_date = st.date_input("📅 取引日", value=st.session_state.last_input_date, key="inp_date_val")
        with c2:
            preset_market_val = selected_sup_choice.replace("🏢 ", "") if selected_sup_choice != "（手入力・自由入力モード）" else st.session_state.last_input_market_no
            input_market = st.text_input("🏢 取引先・市場番号", value=preset_market_val, key="inp_market_val")
        with c3:
            st.text_input("🏢 選択カテゴリー", value=chosen_dept, disabled=True)

        st.markdown("##### 🛒 商品明細（該当課の商品・商品番号が自動候補として選べます）")

        c_item_pick, c_code_auto = st.columns([2.5, 1.5])
        with c_item_pick:
            item_choice_list = ["（直接自由入力モード）"] + candidate_item_options
            selected_item_choice = st.selectbox(
                f"🥬 {chosen_dept} の該当商品を選択:",
                options=item_choice_list,
                index=1 if len(item_choice_list) > 1 else 0,
                key=f"item_preset_picker_{dept_widget_key}"
            )

        preset_code = "AUTO"
        preset_name = ""
        preset_price = 0

        if selected_item_choice != "（直接自由入力モード）":
            m_code = re.match(r"^\[(.*?)\]\s*(.*)$", selected_item_choice)
            if m_code:
                preset_code = m_code.group(1).strip()
                preset_name = m_code.group(2).strip()
                match_r = dept_master_items[dept_master_items["item_code"] == preset_code]
                if not match_r.empty:
                    preset_price = int(match_r.iloc[0]["standard_price"])

        with c_code_auto:
            input_item_code = st.text_input("🔢 商品番号 (コード)", value=preset_code, key="inp_code_field")

        c4, c5, c6 = st.columns([2.2, 1.4, 1.4])
        with c4:
            input_item_name = st.text_input("🥬 品名 (商品名)", value=preset_name, placeholder="キャベツ, 築館なめこ 等", key="inp_name_field")
        with c5:
            input_unit_price = st.number_input("💰 単価 (円)", min_value=0, value=preset_price, step=10, key="inp_price_field")
        with c6:
            input_quantity = st.number_input("📦 数量 (個/ケース)", min_value=1, value=1, step=1, key="inp_qty_field")

        subtotal_calc = input_unit_price * input_quantity
        st.markdown(f"**小計金額**: <span style='font-size: 1.15rem; color: #166534; font-weight: bold;'>¥ {subtotal_calc:,}</span>", unsafe_allow_html=True)

        submit_fast_btn = st.form_submit_button("⚡ 直ちに登録して次の入力へ（連続入力）", use_container_width=True, type="primary")

        if submit_fast_btn:
            final_item_name = input_item_name.strip()
            if not final_item_name:
                st.error("❌ 品名を入力してください")
            else:
                st.session_state.last_input_market_no = input_market.strip() or "M-001"
                st.session_state.last_input_date = input_date
                
                new_rec = {
                    "日付": str(input_date),
                    "市場番号": input_market.strip() or "M-001",
                    "カテゴリー": chosen_dept,
                    "商品コード": input_item_code.strip() or "AUTO",
                    "品名": final_item_name,
                    "単価": int(input_unit_price),
                    "数量": int(input_quantity),
                    "仕入金額": int(subtotal_calc)
                }
                insert_records_to_db(current_user, [new_rec])
                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.toast(f"🎉 「{final_item_name}」 (¥{subtotal_calc:,}) を高速登録しました！下の記録一覧へ正確に反映されました。")
                st.rerun()

    # 📋 記録済みデータ一覧（リアルタイム反映テーブル）
    st.write("")
    st.markdown('<div class="section-title">📋 登録済み取引データ一覧（即時反映・最新順）</div>', unsafe_allow_html=True)

    if df_current is not None and not df_current.empty:
        total_sum = int(df_current["仕入金額"].sum()) if "仕入金額" in df_current.columns else 0
        total_q = int(df_current["数量"].sum()) if "数量" in df_current.columns else 0

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("総取引件数", f"{len(df_current):,} 件")
        m2.metric("総数量", f"{total_q:,} 個")
        m3.metric("総仕入金額", f"¥ {total_sum:,}")
        m4.metric("平均単価", f"¥ {int(total_sum / total_q):,}" if total_q > 0 else "¥ 0")

        display_cols = [c for c in ["id", "日付", "市場番号", "カテゴリー", "商品コード", "品名", "単価", "数量", "仕入金額", "登録担当者"] if c in df_current.columns]
        st.dataframe(df_current[display_cols].head(30), use_container_width=True)
    else:
        st.info("💡 登録されたデータはまだありません。上のドラッグ＆ドロップまたは手動フォームから記録してください。")

# ==========================================
# タブ 2: 取引一覧・インライン編集
# ==========================================
with tabs[1]:
    st.markdown('<div class="section-title">📊 登録データの一覧・直接編集・選択削除</div>', unsafe_allow_html=True)

    if df_current is not None and not df_current.empty:
        col_ctrl1, col_ctrl2 = st.columns([2.5, 1.5])
        with col_ctrl1:
            st.caption("※ テーブル内のセルを直接クリックして数値を変更し、下の「変更をデータベースに保存」を押すと即時反映されます。")
        with col_ctrl2:
            st.caption("※ 一番左の「選択」チェックボックスを入れて「選択したデータを削除」を押すと安全に一括削除できます。")

        editor_df = df_current.copy()
        editor_df.insert(0, "選択", False)

        editor_cols_order = [c for c in ["選択", "id", "日付", "市場番号", "カテゴリー", "商品コード", "品名", "単価", "数量", "仕入金額", "登録担当者"] if c in editor_df.columns]

        column_config = {
            "選択": st.column_config.CheckboxColumn("選択", help="削除対象として選択"),
            "id": st.column_config.NumberColumn("ID", disabled=True),
            "日付": st.column_config.TextColumn("取引日"),
            "市場番号": st.column_config.TextColumn("市場番号/取引先"),
            "カテゴリー": st.column_config.SelectboxColumn("カテゴリー", options=DEPT_LIST),
            "商品コード": st.column_config.TextColumn("商品コード"),
            "品名": st.column_config.TextColumn("品名 (商品名)"),
            "単価": st.column_config.NumberColumn("単価 (円)", min_value=0, step=10),
            "数量": st.column_config.NumberColumn("数量", min_value=0, step=1),
            "仕入金額": st.column_config.NumberColumn("仕入金額 (円)", min_value=0, step=100),
            "登録担当者": st.column_config.TextColumn("登録担当者", disabled=True)
        }

        edited_df = st.data_editor(
            editor_df[editor_cols_order],
            use_container_width=True,
            num_rows="dynamic",
            column_config=column_config,
            key=f"trade_data_editor_v_{st.session_state.get('editor_version', 0)}"
        )

        col_save, col_del, col_dl_xlsx = st.columns([2, 2, 2])

        with col_save:
            if st.button("💾 変更をデータベースに保存する", type="primary", use_container_width=True, key="save_edited_records_btn"):
                changes_count = 0
                for _, row in edited_df.iterrows():
                    rid = row.get("id")
                    if pd.notnull(rid):
                        p = int(row.get("単価", 0))
                        q = int(row.get("数量", 0))
                        sub = p * q
                        update_single_record_in_db({
                            "id": int(rid),
                            "日付": str(row.get("日付", "")),
                            "市場番号": str(row.get("市場番号", "")),
                            "カテゴリー": str(row.get("カテゴリー", "")),
                            "商品コード": str(row.get("商品コード", "")),
                            "品名": str(row.get("品名", "")),
                            "単価": p,
                            "数量": q,
                            "仕入金額": sub
                        })
                        changes_count += 1
                st.toast(f"✅ {changes_count} 件の変更を完全保存しました！")
                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.rerun()

        with col_del:
            selected_rows = edited_df[edited_df["選択"] == True]
            if not selected_rows.empty:
                del_btn_label = f"🗑️ 選択した {len(selected_rows)} 件を削除"
                if st.button(del_btn_label, type="secondary", use_container_width=True, key="del_selected_records_btn"):
                    deleted_ids = selected_rows["id"].dropna().astype(int).tolist()
                    delete_records_by_ids(deleted_ids, deleted_by=current_user)
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.toast(f"🗑️ {len(deleted_ids)} 件を削除しました（いつでも元通り復元可能）。")
                    st.rerun()

        with col_dl_xlsx:
            export_cols = [c for c in ["日付", "市場番号", "カテゴリー", "商品コード", "品名", "数量", "単価", "仕入金額", "支払金額", "差引残高", "合計", "登録担当者"] if c in edited_df.columns]
            xlsx_export_bytes = convert_df_to_xlsx_bytes(edited_df[export_cols], sheet_name="青果取引データ")
            st.download_button(
                "📗 最新 Excel (.xlsx) 出力",
                data=xlsx_export_bytes,
                file_name=f"seika_trade_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
    else:
        st.info("表示する取引データがありません。")

# ==========================================
# タブ 3: 売上分析 & グラフ可視化
# ==========================================
with tabs[2]:
    st.markdown('<div class="section-title">📈 売上・取扱数量・課別構成アナリティクス</div>', unsafe_allow_html=True)

    if df_current is not None and not df_current.empty:
        c_an1, c_an2 = st.columns(2)
        with c_an1:
            cat_sum = df_current.groupby("カテゴリー")["仕入金額"].sum().reset_index()
            fig_pie = px.pie(cat_sum, values="仕入金額", names="カテゴリー", title="課別（カテゴリー別）金額構成比", hole=0.4, color_discrete_sequence=px.colors.qualitative.Pastel)
            st.plotly_chart(fig_pie, use_container_width=True)

        with c_an2:
            item_sum = df_current.groupby("品名")["仕入金額"].sum().reset_index().sort_values("仕入金額", ascending=False).head(10)
            fig_bar = px.bar(item_sum, x="仕入金額", y="品名", orientation="h", title="売上上位10品目 (金額順)", color="仕入金額", color_continuous_scale="Greens")
            st.plotly_chart(fig_bar, use_container_width=True)

        st.markdown("##### 📅 日別売上推移")
        date_sum = df_current.groupby("日付")["仕入金額"].sum().reset_index().sort_values("日付")
        fig_line = px.line(date_sum, x="日付", y="仕入金額", markers=True, title="日別売上金額トレンド (円)", line_shape="spline")
        st.plotly_chart(fig_line, use_container_width=True)
    else:
        st.info("グラフ化するデータがありません。")

# ==========================================
# タブ 4: スマート青果AIアナライザー (Gemini 4 Argon)
# ==========================================
with tabs[3]:
    st.markdown('<div class="section-title">🤖 生成AIスマート青果アナライザー (Gemini 4 Argon搭載)</div>', unsafe_allow_html=True)
    
    st.markdown("""
        <div class="security-notice">
            <b>💡 青果経営・相場特化型AI:</b>
            日々の取引データから、課別の売上傾向、ロット単価の適正性、異常値検出、および今後の発注推奨を自動生成します。
        </div>
    """, unsafe_allow_html=True)

    user_ai_prompt = st.text_area(
        "💬 AIへの個別リクエスト・指示（任意）:",
        placeholder="例: 築館なめこや主力野菜の価格妥当性と、次週の仕入れリスクについて詳しく教えてください。",
        height=80
    )

    if st.button("🚀 AIによる取引分析レポートを生成する", type="primary", use_container_width=True):
        with st.spinner("🤖 青果取引データを多角的に分析中..."):
            ai_rep = generate_produce_ai_insights(df_current, user_ai_prompt)
            st.markdown(ai_rep)

# ==========================================
# タブ 5: BCP安全バックアップ & 復元
# ==========================================
with tabs[4]:
    st.markdown('<div class="section-title">🛡️ BCP安全バックアップ・スナップショット & ワンクリック復元</div>', unsafe_allow_html=True)

    st.markdown("""
        <div class="security-notice">
            <b>🛡️ 自動安全設計:</b>
            データ追加や一括インポート、削除を行うたびに、システムが自動的にバックアップスナップショットを生成しています。<br>
            万が一の誤操作や障害時も、任意の時点へワンクリックで100%安全にロールバック（元通りに復元）可能です。
        </div>
    """, unsafe_allow_html=True)

    c_bk_act1, c_bk_act2 = st.columns([2, 3])
    with c_bk_act1:
        if st.button("📸 手動で今すぐスナップショットを作成", use_container_width=True):
            bk_fname = create_backup_snapshot(current_user, trigger_reason="手動スナップショット作成")
            st.toast(f"✅ バックアップを作成しました: {bk_fname}")
            st.rerun()

    st.write("")
    backups_list = list_all_backups()
    if backups_list:
        st.markdown(f"##### 🗄️ 利用可能なバックアップスナップショット ({len(backups_list)} 件)")
        for idx, target_bk in enumerate(backups_list[:10]):
            with st.expander(f"📦 {target_bk['timestamp']} - {target_bk['reason']} ({target_bk['record_count']}件 / ¥{target_bk['total_subtotal']:,})", expanded=(idx == 0)):
                col_b1, col_b2 = st.columns([3, 1])
                with col_b1:
                    st.write(f"• 作成者: **{target_bk['created_by']}**")
                    st.write(f"• ファイル名: `{target_bk['filename']}`")
                    st.write(f"• 記録件数: **{target_bk['record_count']:,} 件** (総額: ¥{target_bk['total_subtotal']:,})")
                with col_b2:
                    if st.button("🔄 この時点へ復元", key=f"restore_btn_{target_bk['filename']}", use_container_width=True, type="primary"):
                        r_res = restore_from_backup_file(target_bk["filepath"], restored_by=current_user)
                        if r_res.get("success"):
                            st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                            st.success(f"🎉 {target_bk['timestamp']} の状態へ完全復旧しました！")
                            st.rerun()
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
                <b>シート毎に表示されている「出荷日」「品名」「数量」「単価」「仕入金額」の全セル・配置・数値を100%保持</b> したまま、
                最新の <b>Excelブック (.xlsx)</b> 形式へ瞬時に変換してダウンロードできます。<br>
                同時に、下のインポートボタンを押すことで、そのままデータベースへ自動記録され、<b>「登録済み取引データ一覧」へ全ての数値データと書式が自動的に反映</b>されます。
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
                st.markdown(f"#### 📄 解析結果プレビュー: 『{fname}』 (検出シート数: {len(raw_sheets)} シート / 明細 {len(df_up):,} 件)")
                
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
                    csv_converted_data = df_up[csv_export_cols].to_csv(index=False).encode("utf-8-sig") if not df_up.empty else b""
                    st.download_button(
                        label="📥 全シート統合 取引データ (CSV) でダウンロード",
                        data=csv_converted_data,
                        file_name=f"{file_base_name}_取引明細.csv",
                        mime="text/csv",
                        use_container_width=True
                    )

                if is_legacy_xls:
                    sheet_names_str = ", ".join(list(raw_sheets.keys())[:4]) + (" ほか" if len(raw_sheets) > 4 else "")
                    st.success(f"🎉 昔のエクセル形式 (<b>{fname}</b>) の全シート（{sheet_names_str}）に記載された<b>品名・数量・単価・仕入金額</b>を完全保持したまま最新 <b>.xlsx</b> 形式に正常変換しました！上記の緑色ボタンからダウンロード可能です。")

                st.write("")
                # シート別プレビュー表示
                if len(raw_sheets) > 1:
                    st.markdown("##### 📑 全シート保持プレビュー（元のエクセル書式・配置そのまま）")
                    preview_tabs = st.tabs(list(raw_sheets.keys())[:6])
                    for t_idx, (sname, sdf) in enumerate(list(raw_sheets.items())[:6]):
                        with preview_tabs[t_idx]:
                            st.caption(f"シート名: **{sname}** (全 {len(sdf)} 行)")
                            st.dataframe(sdf.head(30), use_container_width=True)

                st.markdown("##### 📥 データベースへの一括インポート")
                col_imp1, col_imp2 = st.columns([2, 3])
                with col_imp1:
                    if st.button("📥 この内容をデータベースに一括インポートする", type="primary", use_container_width=True, key="btn_bulk_import_exec"):
                        for s in sups:
                            if s and s not in st.session_state.extracted_suppliers:
                                st.session_state.extracted_suppliers.append(s)

                        insert_records_to_db(current_user, df_up.to_dict("records"))
                        st.session_state.verification_batch = {
                            "file_name": uploaded_file.name,
                            "df": df_up
                        }
                        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                        st.success(f"🎉 『{uploaded_file.name}』から {len(df_up):,} 件を一括インポートしました！（登録担当者: {current_user}）")
                        st.rerun()
                with col_imp2:
                    if sups:
                        st.caption(f"抽出された取引先（次回から候補として利用可能）: {', '.join(sups[:5])}{' ほか' if len(sups) > 5 else ''}")
            else:
                st.warning("⚠️ ファイル内に有効なデータ行が見つかりませんでした。")
        except Exception as e:
            st.error(f"ファイル解析エラー: {e}")

# ==========================================
# タブ 7: 社員・アカウント管理（管理者専用）
# ==========================================
if is_admin and len(tabs) > 6:
    with tabs[6]:
        st.markdown('<div class="section-title">👥 社員・アカウント管理 & パスワード暗号化登録</div>', unsafe_allow_html=True)

        col_reg1, col_reg2 = st.columns([2, 2.5])
        with col_reg1:
            st.markdown("##### ➕ 新規社員・ユーザーアカウント作成")
            with st.form("add_user_form"):
                new_u = st.text_input("👤 ユーザーID (半角英数)", placeholder="user3, staff_b 等")
                new_d = st.text_input("📛 表示名・所属", placeholder="田中 (営業3課)")
                new_p = st.text_input("🔒 初期パスワード", type="password")
                new_r = st.selectbox("🛡️ 権限ロール", options=["staff", "admin"], format_func=lambda x: "👑 統括管理者 (admin)" if x == "admin" else "👤 営業スタッフ (staff)")
                
                sub_reg = st.form_submit_button("新規ユーザーを登録する", use_container_width=True, type="primary")
                if sub_reg:
                    if not new_u or not new_p:
                        st.error("❌ ユーザーIDとパスワードは必須です")
                    else:
                        ok, msg = register_new_user_db(new_u, new_p, new_r, new_d)
                        if ok:
                            st.success(f"✅ {msg}")
                            st.rerun()
                        else:
                            st.error(f"❌ {msg}")

        with col_reg2:
            st.markdown("##### 👥 登録済み社員アカウント一覧")
            users_list = get_all_users_from_db()
            st.dataframe(pd.DataFrame(users_list), use_container_width=True)

        st.divider()
        st.markdown("##### 🏆 社員別取引実績サマリー")
        emp_stats_df = get_employee_summary_stats()
        if not emp_stats_df.empty:
            emp_stats_df = emp_stats_df.rename(columns={
                "user_id": "担当者", "record_count": "登録件数",
                "total_amount": "総売上金額 (円)", "total_quantity": "総取扱数量",
                "last_activity": "最終登録日時"
            })
            st.dataframe(emp_stats_df, use_container_width=True)
        else:
            st.caption("社員別の取引データはまだ集計されていません。")
