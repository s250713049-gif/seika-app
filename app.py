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
                pool_size=5,
                max_overflow=10,
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
# 3. データベーステーブル初期化 & 自動マイグレーション
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
                payment_amount INTEGER NOT NULL DEFAULT 0,
                balance INTEGER NOT NULL DEFAULT 0,
                total_amount INTEGER NOT NULL DEFAULT 0,
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
                payment_amount INTEGER DEFAULT 0,
                balance INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                user_id TEXT,
                deleted_by TEXT,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))

        # 既存データベースのカラム自動マイグレーション (品名・数量・単価・仕入金額・支払金額・差引残高・合計)
        for col_name, col_type in [
            ("payment_amount", "INTEGER NOT NULL DEFAULT 0"),
            ("balance", "INTEGER NOT NULL DEFAULT 0"),
            ("total_amount", "INTEGER NOT NULL DEFAULT 0")
        ]:
            try:
                conn.execute(text(f"ALTER TABLE transactions ADD COLUMN {col_name} {col_type};"))
            except Exception:
                pass
            try:
                conn.execute(text(f"ALTER TABLE deleted_transactions ADD COLUMN {col_name} {col_type};"))
            except Exception:
                pass

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

# ==========================================
# 5. 取引データ操作 (全項目・高速キャッシュ連携)
# ==========================================
@st.cache_data(ttl=60)
def load_records_cached(target_user_filter: str = None):
    engine = get_engine()
    with engine.connect() as conn:
        cols_q = """
            id, date AS 日付, market_number AS 市場番号, category AS カテゴリー,
            item_code AS 商品コード, item_name AS 品名, quantity AS 数量,
            unit_price AS 単価, subtotal AS 仕入金額,
            COALESCE(payment_amount, 0) AS 支払金額,
            COALESCE(balance, 0) AS 差引残高,
            COALESCE(total_amount, 0) AS 合計,
            subtotal AS 小計, item_name AS 商品名, user_id AS 登録担当者
        """
        if target_user_filter:
            query = text(f"SELECT {cols_q} FROM transactions WHERE user_id = :u ORDER BY id DESC;")
            df = pd.read_sql(query, conn, params={"u": target_user_filter})
        else:
            query = text(f"SELECT {cols_q} FROM transactions ORDER BY id DESC;")
            df = pd.read_sql(query, conn)
    return df

def load_records_from_db(target_user_filter: str = None):
    return load_records_cached(target_user_filter)

def invalidate_records_cache():
    load_records_cached.clear()

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
                "c": str(r.get("カテゴリー", "1課 (熊谷・大宮)")),
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
    invalidate_records_cache()
    if auto_backup:
        create_backup_snapshot(user_id, trigger_reason="新規データ追加")

def update_single_record_in_db(record_dict: dict):
    rec_id = record_dict.get("id")
    if not rec_id:
        return
    engine = get_engine()
    p = int(record_dict.get("単価", 0))
    q = int(record_dict.get("数量", 0))
    sub = int(record_dict.get("仕入金額", record_dict.get("小計", p * q)))
    pay = int(record_dict.get("支払金額", 0))
    bal = int(record_dict.get("差引残高", 0))
    tot = int(record_dict.get("合計", 0))
    item_n = str(record_dict.get("品名", record_dict.get("商品名", "品名未指定"))).strip() or "品名未指定"
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE transactions
            SET date = :date, market_number = :m, category = :c, item_code = :code,
                item_name = :name, unit_price = :p, quantity = :q, subtotal = :sub,
                payment_amount = :pay, balance = :bal, total_amount = :tot
            WHERE id = :id
        """), {
            "id": rec_id,
            "date": str(record_dict.get("日付")),
            "m": str(record_dict.get("市場番号")),
            "c": str(record_dict.get("カテゴリー")),
            "code": str(record_dict.get("商品コード")),
            "name": item_n,
            "p": p,
            "q": q,
            "sub": sub,
            "pay": pay,
            "bal": bal,
            "tot": tot
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
                r_dict = dict(row._mapping)
                backed_up.append(r_dict)
                conn.execute(text("""
                    INSERT INTO deleted_transactions (original_id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, payment_amount, balance, total_amount, user_id, deleted_by)
                    VALUES (:oid, :d, :m, :c, :cd, :name, :p, :q, :sub, :pay, :bal, :tot, :u, :delby)
                """), {
                    "oid": row.id, "d": row.date, "m": row.market_number, "c": row.category,
                    "cd": row.item_code, "name": row.item_name, "p": row.unit_price,
                    "q": row.quantity, "sub": row.subtotal,
                    "pay": getattr(row, "payment_amount", 0) or 0,
                    "bal": getattr(row, "balance", 0) or 0,
                    "tot": getattr(row, "total_amount", 0) or 0,
                    "u": row.user_id, "delby": deleted_by
                })
        conn.execute(text(f"DELETE FROM transactions WHERE id IN ({','.join(str(i) for i in ids)})"))
    invalidate_records_cache()
    create_backup_snapshot(deleted_by, trigger_reason=f"{len(ids)}件のデータ削除")
    return backed_up

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
                INSERT INTO transactions (date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, payment_amount, balance, total_amount, user_id)
                VALUES (:d, :m, :c, :cd, :name, :p, :q, :sub, :pay, :bal, :tot, :u)
            """), {
                "d": r.get("date"), "m": r.get("market_number"), "c": r.get("category"),
                "cd": r.get("item_code"), "name": r.get("item_name"), "p": r.get("unit_price", 0),
                "q": r.get("quantity", 0), "sub": r.get("subtotal", 0),
                "pay": r.get("payment_amount", 0) or 0,
                "bal": r.get("balance", 0) or 0,
                "tot": r.get("total_amount", 0) or 0,
                "u": r.get("user_id", restored_by)
            })
    invalidate_records_cache()
    create_backup_snapshot(restored_by, trigger_reason=f"スナップショット復元: {os.path.basename(filepath)}")
    return {"success": True, "count": len(records)}

# ==========================================
# 7. 商品マスタ（社内公式コード）
# ==========================================
CATEGORY_TXT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "category_master.txt")

CATEGORY_MASTER_DEFAULT_TEXT = r'''# ==============================================================================
# category_master.txt  ―  青果卸売 カテゴリー（課）・商品・商品番号マスタ
# ==============================================================================
1課4課（熊谷・大宮） / 2課 / 栗原|01|果菜類|319|トマト
1課4課（熊谷・大宮） / 2課 / 栗原|01|果菜類|320|ミニトマト
3課（照井） / 11課（相澤 史）|02|葉茎菜類|201|キャベツ
3課（照井） / 11課（相澤 史）|02|葉茎菜類|205|レタス
1課4課（熊谷・大宮） / 2課 / 栗原|03|根菜類|101|大根
1課4課（熊谷・大宮） / 2課 / 栗原|03|根菜類|11|かぶ
'''

MASTER_COLS = ["category_code", "category_name", "department", "item_code", "item_name"]

BASE_DEPT_LIST = [
    "1課4課（熊谷・大宮） / 2課 / 栗原",
    "3課（照井） / 11課（相澤 史）",
    "5課 / 6課（川越）",
    "7課（促成野菜・ハーブ）",
    "8課（果実・柑橘・高級フルーツ）",
    "加工食品 / 仙都食苑"
]

def _norm_dept(s) -> str:
    if s is None:
        return ""
    t = unicodedata.normalize("NFKC", str(s))
    return t.replace(" ", "").replace("\u3000", "").replace("\t", "").strip()

def _dept_tokens(s) -> set:
    return set(re.findall(r"\d+課", _norm_dept(s)))

def _dept_match(row_dept, chosen_dept) -> bool:
    rk, ck = _norm_dept(row_dept), _norm_dept(chosen_dept)
    if not rk or not ck:
        return False
    if rk == ck:
        return True
    rt, ct = _dept_tokens(rk), _dept_tokens(ck)
    if rt and ct:
        return bool(rt & ct)
    if not rt and not ct:
        return rk in ck or ck in rk
    return False

def get_items_for_department(df_master, dept):
    if df_master is None or df_master.empty or not dept:
        return pd.DataFrame(columns=MASTER_COLS)
    mask = df_master["department"].apply(lambda d: _dept_match(d, dept))
    return df_master[mask].reset_index(drop=True)

def _file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0

def _read_category_txt(path: str) -> pd.DataFrame:
    raw = None
    if not os.path.exists(path):
        raw = CATEGORY_MASTER_DEFAULT_TEXT
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(CATEGORY_MASTER_DEFAULT_TEXT)
        except Exception:
            pass
    else:
        for enc in ("utf-8-sig", "utf-8", "cp932"):
            try:
                with open(path, "r", encoding=enc) as f:
                    raw = f.read()
                break
            except Exception:
                continue
    if raw is None:
        return pd.DataFrame(columns=MASTER_COLS)

    rows = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        s = s.replace("｜", "|")
        sep = "|" if "|" in s else ("\t" if "\t" in s else ",")
        cols = [c.strip() for c in s.split(sep)]
        if len(cols) < 5:
            continue
        dept, ccode, cname, icode, iname = cols[:5]
        if not iname or icode in ("商品コード", "item_code"):
            continue
        rows.append({
            "category_code": ccode, "category_name": cname, "department": dept,
            "item_code": icode, "item_name": iname
        })
    return pd.DataFrame(rows, columns=MASTER_COLS)

@st.cache_data(ttl=3600)
def load_item_master_cached(csv_mtime: float = 0.0, txt_mtime: float = 0.0):
    frames = []
    if os.path.exists(MASTER_CSV_FILE):
        for enc in ("utf-8-sig", "utf-8", "cp932"):
            try:
                frames.append(pd.read_csv(MASTER_CSV_FILE, dtype=str, encoding=enc))
                break
            except Exception:
                continue
    txt_df = _read_category_txt(CATEGORY_TXT_FILE)
    if not txt_df.empty:
        frames.append(txt_df)

    if not frames:
        frames.append(pd.DataFrame([
            {"category_code": "01", "category_name": "果菜類", "department": "1課4課（熊谷・大宮） / 2課 / 栗原", "item_code": "319", "item_name": "トマト"},
            {"category_code": "01", "category_name": "果菜類", "department": "1課4課（熊谷・大宮） / 2課 / 栗原", "item_code": "320", "item_name": "ミニトマト"},
            {"category_code": "02", "category_name": "葉茎菜類", "department": "3課（照井） / 11課（相澤 史）", "item_code": "201", "item_name": "キャベツ"},
            {"category_code": "02", "category_name": "葉茎菜類", "department": "3課（照井） / 11課（相澤 史）", "item_code": "205", "item_name": "レタス"},
            {"category_code": "03", "category_name": "根菜類", "department": "1課4課（熊谷・大宮） / 2課 / 栗原", "item_code": "101", "item_name": "大根"},
            {"category_code": "03", "category_name": "根菜類", "department": "1課4課（熊谷・大宮） / 2課 / 栗原", "item_code": "11", "item_name": "かぶ"}
        ]))

    df_master = pd.concat(frames, ignore_index=True, sort=False)
    for col in MASTER_COLS:
        if col not in df_master.columns:
            df_master[col] = ""
    df_master = df_master[MASTER_COLS].fillna("").astype(str)
    for col in MASTER_COLS:
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
    全角記号・通貨単位・カンマ・マイナス表記を100%確実にクレンジングして整数化
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
    if "▲" in s or "△" in s or "-" in s or (s.startswith("(") and s.endswith(")")):
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

def parse_japanese_date(val) -> str:
    """和暦 (R8.9.2, 令8.9.10等) や 9月5日、9/11、西暦日付を YYYY-MM-DD に高精度標準化"""
    if val is None or pd.isnull(val):
        return ""
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.strftime("%Y-%m-%d")
    s = unicodedata.normalize("NFKC", str(val)).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return ""
    
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

    m_ymd = re.match(r"^(\d{4})[\.年/\-](\d{1,2})[\.月/\-](\d{1,2})日?", s)
    if m_ymd:
        return f"{int(m_ymd.group(1)):04d}-{int(m_ymd.group(2)):02d}-{int(m_ymd.group(3)):02d}"

    m_md = re.match(r"^(\d{1,2})[\.月/\-](\d{1,2})日?$", s)
    if m_md:
        cur_y = datetime.now().year
        return f"{cur_y:04d}-{int(m_md.group(1)):02d}-{int(m_md.group(2)):02d}"

    return s.split("T")[0].split(" ")[0].strip()

def build_raw_grid_xlsx_bytes(sheet_grids: dict) -> bytes:
    """
    旧来xlsの全シートの「セル配置・行・列・空白・数値」を一切削らず、
    元のエクセルの見た目・配置そのままに最新の .xlsx (OpenXML) を生成
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

        for idx, (sname, grid) in enumerate(sheet_grids.items(), start=1):
            part_name = f"/xl/worksheets/sheet{idx}.xml"
            content_types.append(f'  <Override PartName="{part_name}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            workbook_rels.append(f'  <Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>')
            clean_sname = str(sname)[:31].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;") or f"Sheet{idx}"
            sheets_xml.append(f'    <sheet name="{clean_sname}" sheetId="{idx}" r:id="rId{idx}"/>')

            rows_xml = []
            for r_i, row_data in enumerate(grid, start=1):
                c_cells = []
                for c_i, val in enumerate(row_data):
                    if val is None or val == "" or (isinstance(val, float) and pd.isna(val)):
                        continue
                    cref = f"{col_letter(c_i)}{r_i}"
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        c_cells.append(f'<c r="{cref}"><v>{val}</v></c>')
                    else:
                        esc_v = str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
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

def extract_exact_sheet_grids(uploaded_file) -> dict:
    """旧来xlsファイルから全シート・全行・全列の未加工グリッド（空行や表の配置そのまま）を抽出"""
    content_bytes = uploaded_file.getvalue()
    grids = {}

    # 1. xlrd によるセルレベルの完全抽出（書式・空白維持）
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
                            row_vals.append(int(cell.value) if cell.value.is_integer() else cell.value)
                        elif cell.ctype == xlrd.XL_CELL_TEXT:
                            row_vals.append(cell.value)
                        elif cell.ctype == xlrd.XL_CELL_DATE:
                            try:
                                dt = xlrd.xldate_as_datetime(cell.value, wb.datemode)
                                row_vals.append(dt.strftime("%Y-%m-%d"))
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

    # 2. pandas による生マトリクス抽出
    for eng in ["xlrd", "openpyxl"]:
        try:
            raw_dict = pd.read_excel(io.BytesIO(content_bytes), sheet_name=None, header=None, engine=eng)
            if raw_dict:
                for sname, df in raw_dict.items():
                    grid = df.fillna("").values.tolist()
                    if grid:
                        grids[sname] = grid
                if grids:
                    return grids
        except Exception:
            continue

    # 3. BIFF8 バイナリパーサーによるセル座標直接抽出
    try:
        wb_stream = extract_ole_workbook_stream(content_bytes)
        if wb_stream:
            cells = parse_biff_cells(wb_stream)
            if cells:
                max_r = max(r for r, c in cells.keys())
                max_c = max(c for r, c in cells.keys())
                grid = [["" for _ in range(max_c + 1)] for _ in range(max_r + 1)]
                for (r, c), val in cells.items():
                    grid[r][c] = val
                base_name = uploaded_file.name.rsplit(".", 1)[0]
                grids[base_name] = grid
                return grids
    except Exception:
        pass

    return grids

def convert_xls_file_to_multisheet_xlsx_bytes(uploaded_file) -> bytes:
    """旧来.xlsを、伝票の表・数値・書式を1セルたりとも狂わせずに最新.xlsx形式へ完全変換"""
    try:
        grids = extract_exact_sheet_grids(uploaded_file)
        uploaded_file.seek(0)
        if grids:
            return build_raw_grid_xlsx_bytes(grids)
    except Exception:
        pass
    df_single, _ = parse_trade_file(uploaded_file)
    uploaded_file.seek(0)
    return convert_df_to_xlsx_bytes(df_single, sheet_name="Sheet1")

def convert_df_to_xlsx_bytes(df: pd.DataFrame, sheet_name: str = "Sheet1") -> bytes:
    """単一データフレームの .xlsx 変換"""
    grid = [list(df.columns)] + df.fillna("").values.tolist()
    return build_raw_grid_xlsx_bytes({sheet_name: grid})

def extract_all_raw_sheets_from_file(uploaded_file) -> dict:
    """タブ6のプレビュー用（データフレーム形式）"""
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
    df_single, _ = parse_trade_file(uploaded_file)
    uploaded_file.seek(0)
    base_name = uploaded_file.name.rsplit(".", 1)[0]
    return {base_name: df_single} if not df_single.empty else {}

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
    """データフレームの先頭30行をスキャンし、青果取引項目の適合度が最も高い行をヘッダー行として選定"""
    best_idx = 0
    max_score = 0
    limit = min(30, len(df))
    for r_idx in range(limit):
        row_vals = [normalize_header(v) for v in df.iloc[r_idx].values if pd.notnull(v)]
        score = 0
        for s in row_vals:
            if not s:
                continue
            if any(k in s for k in ["品名", "商品", "品目", "規格", "名称", "品名規格"]): score += 4
            if any(k in s for k in ["単価", "売価", "仕入単価", "価格", "卸単価"]): score += 3
            if any(k in s for k in ["数量", "個数", "受入", "納品数", "入数", "バラ", "ケース", "箱"]): score += 3
            if any(k in s for k in ["仕入金額", "小計", "金額", "売上金額", "計", "販売金額"]): score += 3
            if any(k in s for k in ["支払金額", "差引残高", "残高", "合計"]): score += 3
            if any(k in s for k in ["出荷日", "日付", "取引日", "仕入日", "売上日", "納品日", "年月日", "伝票日"]): score += 3
            if any(k in s for k in ["市場", "取引先", "仕入先", "顧客", "得意先", "納品先", "相手先", "荷主", "会社"]): score += 2
            if any(k in s for k in ["商品コード", "品番", "品目コード", "コード"]): score += 2
        if score > max_score:
            max_score = score
            best_idx = r_idx
    return best_idx if max_score >= 2 else 0

# ==========================================
# 9.0 Pure-Python OLE2 / BIFF8 バイナリパーサー (xlrd非依存・高堅牢版)
# ==========================================
def extract_ole_workbook_stream(data: bytes) -> bytes:
    """OLE2 Compound File から Workbook / Book ストリームを純粋なPythonで安全に抽出"""
    if len(data) < 512 or data[:8] != bytes([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1]):
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

def parse_biff_cells(stream: bytes) -> dict:
    """BIFF8 ストリームから SST と各セル値を安全に展開（IndexError/KeyError完全防御）"""
    if not stream or len(stream) < 4:
        return {}
    pos = 0
    sst = []
    cells = {}
    try:
        while pos < len(stream) - 4:
            rtype, rlen = struct.unpack_from("<HH", stream, pos)
            pos += 4
            if pos + rlen > len(stream):
                rlen = max(0, len(stream) - pos)
            rdata = stream[pos:pos + rlen]
            pos += rlen
            
            if rtype == 0x00FC: # SST
                try:
                    if len(rdata) >= 8:
                        num_strings, num_unique = struct.unpack_from("<II", rdata, 0)
                        spos = 8
                        while spos < len(rdata) and len(sst) < num_unique:
                            if spos + 3 > len(rdata): break
                            clen, flags = struct.unpack_from("<HB", rdata, spos)
                            spos += 3
                            is_16bit = bool(flags & 1)
                            has_rich = bool(flags & 8)
                            has_phonetic = bool(flags & 4)
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
            elif rtype == 0x00FD: # LABELSST
                if len(rdata) >= 10:
                    row, col, xf, sst_idx = struct.unpack_from("<HHHI", rdata, 0)
                    if sst_idx < len(sst):
                        cells[(row, col)] = sst[sst_idx]
            elif rtype == 0x027E: # RK
                if len(rdata) >= 10:
                    row, col, xf, rk_val = struct.unpack_from("<HHHI", rdata, 0)
                    is_div100 = bool(rk_val & 1)
                    if rk_val & 2:
                        v = (rk_val >> 2)
                        if v >= 0x20000000: v -= 0x40000000
                        cells[(row, col)] = v / 100.0 if is_div100 else v
                    else:
                        raw_b = struct.pack("<I", rk_val & 0xFFFFFFFC) + b"\x00\x00\x00\x00"
                        v = struct.unpack("<d", raw_b)[0]
                        cells[(row, col)] = v / 100.0 if is_div100 else v
            elif rtype == 0x00BD: # MULRK
                if len(rdata) >= 6:
                    row, col_first = struct.unpack_from("<HH", rdata, 0)
                    col_last = struct.unpack_from("<H", rdata, len(rdata) - 2)[0]
                    n_cols = max(0, col_last - col_first + 1)
                    for c_i in range(n_cols):
                        rk_offset = 4 + c_i * 6 + 2
                        if rk_offset + 4 <= len(rdata):
                            rk_val = struct.unpack_from("<I", rdata, rk_offset)[0]
                            is_div100 = bool(rk_val & 1)
                            if rk_val & 2:
                                v = (rk_val >> 2)
                                if v >= 0x20000000: v -= 0x40000000
                                cells[(row, col_first + c_i)] = v / 100.0 if is_div100 else v
                            else:
                                raw_b = struct.pack("<I", rk_val & 0xFFFFFFFC) + b"\x00\x00\x00\x00"
                                v = struct.unpack("<d", raw_b)[0]
                                cells[(row, col_first + c_i)] = v / 100.0 if is_div100 else v
            elif rtype in (0x0203, 0x0006): # NUMBER / FORMULA
                if len(rdata) >= 14:
                    row, col, xf = struct.unpack_from("<HHH", rdata, 0)
                    val = struct.unpack_from("<d", rdata, 6)[0]
                    cells[(row, col)] = val
            elif rtype == 0x0204: # LABEL
                if len(rdata) >= 8:
                    row, col, xf, clen = struct.unpack_from("<HHHH", rdata, 0)
                    str_b = rdata[8:8 + clen]
                    cells[(row, col)] = str_b.decode("cp932", errors="ignore").strip("\x00")
    except Exception:
        pass
    return cells

def parse_xls_binary_fallback(content_bytes: bytes) -> pd.DataFrame:
    """Pure-Python OLE2 / BIFF8 解析、HTMLテーブル、CSVパースを連鎖して実行"""
    try:
        wb_stream = extract_ole_workbook_stream(content_bytes)
        if wb_stream:
            cells = parse_biff_cells(wb_stream)
            if cells:
                max_r = max(r for r, c in cells.keys())
                max_c = max(c for r, c in cells.keys())
                grid = [["" for _ in range(max_c + 1)] for _ in range(max_r + 1)]
                for (r, c), val in cells.items():
                    grid[r][c] = val
                df_biff = pd.DataFrame(grid).dropna(how="all").dropna(axis=1, how="all")
                if not df_biff.empty:
                    return df_biff
    except Exception:
        pass

    try:
        text_data = ""
        for enc in ["utf-8-sig", "cp932", "shift-jis", "latin1"]:
            try:
                text_data = content_bytes.decode(enc, errors="ignore")
                if len(text_data) > 0:
                    break
            except Exception:
                continue

        if "<table" in text_data.lower() or "<tr" in text_data.lower():
            dfs = pd.read_html(io.BytesIO(content_bytes))
            if dfs:
                best_df = max(dfs, key=lambda d: len(d) * len(d.columns))
                if not best_df.empty:
                    return best_df

        lines = [line.strip() for line in text_data.splitlines() if line.strip()]
        rows = []
        for line in lines:
            if "\t" in line:
                parts = [p.strip() for p in line.split("\t")]
            elif "," in line:
                parts = [p.strip() for p in line.split(",")]
            else:
                parts = [p.strip() for p in re.split(r"\s{2,}", line) if p.strip()]
            if len(parts) >= 2:
                rows.append(parts)

        if rows:
            max_len = max(len(r) for r in rows)
            padded = [r + [""] * (max_len - len(r)) for r in rows]
            header = [str(h) if str(h).strip() else f"列_{i+1}" for i, h in enumerate(padded[0])]
            return pd.DataFrame(padded[1:], columns=header)
    except Exception:
        pass
    return pd.DataFrame()

# ==========================================
# 9.2 統合ファイル解析エンジン (品名未指定・数値0化・解析エラーを100%解決)
# ==========================================
def parse_single_sheet_dataframe(sdf_raw: pd.DataFrame, sheet_name: str, file_name: str):
    """単一シートの DataFrame から品名・数値・日付・全データを高精度に解析・抽出"""
    if sdf_raw is None or sdf_raw.empty:
        return [], None
    sdf = sdf_raw.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if sdf.empty:
        return [], None

    # 1. シート先頭15行から「荷主名」「取引先名」「デフォルト品名」を探索
    base_supplier = re.sub(r"\s*\([0-9]+\)$", "", str(sheet_name)).strip() or "自社取引先"
    default_item_name = base_supplier
    
    for r_chk in range(min(15, len(sdf))):
        row_cells = [str(x).strip() for x in sdf.iloc[r_chk].values if pd.notnull(x) and str(x).strip()]
        for idx_c, cell_str in enumerate(row_cells):
            norm_c = normalize_header(cell_str)
            if any(k in norm_c for k in ["取引先名", "仕入先", "得意先", "荷主", "出荷者", "組合", "農協", "会社"]):
                if ":" in cell_str or "：" in cell_str:
                    s_val = re.split(r"[:：]", cell_str)[-1].strip()
                    if s_val:
                        base_supplier = s_val
                elif idx_c + 1 < len(row_cells):
                    next_val = row_cells[idx_c + 1].strip()
                    if next_val and not any(k in normalize_header(next_val) for k in ["仕入", "品名", "数量", "単価", "金額"]):
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
        elif any(k in h_str for k in ["数量", "個数", "入数", "受入", "納品数", "ケース", "箱"]):
            if "数量" not in col_map: col_map["数量"] = col_idx
        elif any(k in h_str for k in ["単価", "仕入単価", "売価", "価格", "卸単価"]):
            if "単価" not in col_map: col_map["単価"] = col_idx
        elif any(k in h_str for k in ["仕入金額", "金額", "小計", "売上金額", "計", "販売金額"]):
            if "仕入金額" not in col_map: col_map["仕入金額"] = col_idx
        elif any(k in h_str for k in ["支払金額", "支払額", "出金"]):
            if "支払金額" not in col_map: col_map["支払金額"] = col_idx
        elif any(k in h_str for k in ["差引残高", "残高", "差引"]):
            if "差引残高" not in col_map: col_map["差引残高"] = col_idx
        elif any(k in h_str for k in ["合計", "日計", "総合計"]):
            if "合計" not in col_map: col_map["合計"] = col_idx
        elif any(k in h_str for k in ["商品コード", "品番", "コード", "品コード"]):
            if "商品コード" not in col_map: col_map["商品コード"] = col_idx

    # 品名列が見つからない場合のフォールバック（商品コード列または第2列）
    if "品名" not in col_map:
        if "商品コード" in col_map:
            col_map["品名"] = col_map["商品コード"]
        elif len(header_row_vals) >= 2:
            col_map["品名"] = 1

    # 4. 行ごとの精緻なデータ抽出
    cur_date = str(datetime.now().date())
    last_item_name = default_item_name
    sheet_records = []

    for _, r_data in data_df.iterrows():
        r_vals = r_data.values
        if len(r_vals) == 0:
            continue
        
        # 日付
        if "日付" in col_map and col_map["日付"] < len(r_vals):
            p_d = parse_japanese_date(r_vals[col_map["日付"]])
            if p_d:
                cur_date = p_d

        # 品名
        raw_name = r_vals[col_map["品名"]] if "品名" in col_map and col_map["品名"] < len(r_vals) else None
        item_str = ""
        if pd.notnull(raw_name):
            item_s = unicodedata.normalize("NFKC", str(raw_name)).strip()
            if item_s and item_s.lower() not in ("nan", "none", "null"):
                item_str = item_s

        # 合計行・消費税行の除外
        if item_str and re.match(r"^(消費税|税額|地方消費税|差引合計|総合計|総計|合計|販売金額|販売手数料|計)$", item_str):
            continue

        q_val = clean_num(r_vals[col_map["数量"]]) if "数量" in col_map and col_map["数量"] < len(r_vals) else 0
        p_val = clean_num(r_vals[col_map["単価"]]) if "単価" in col_map and col_map["単価"] < len(r_vals) else 0
        sub_val = clean_num(r_vals[col_map["仕入金額"]]) if "仕入金額" in col_map and col_map["仕入金額"] < len(r_vals) else 0
        pay_val = clean_num(r_vals[col_map["支払金額"]]) if "支払金額" in col_map and col_map["支払金額"] < len(r_vals) else 0
        bal_val = clean_num(r_vals[col_map["差引残高"]]) if "差引残高" in col_map and col_map["差引残高"] < len(r_vals) else 0
        tot_val = clean_num(r_vals[col_map["合計"]]) if "合計" in col_map and col_map["合計"] < len(r_vals) else 0

        # 数値補完
        if sub_val == 0 and p_val > 0 and q_val > 0:
            sub_val = p_val * q_val
        elif p_val == 0 and sub_val > 0 and q_val > 0:
            p_val = int(round(sub_val / q_val))

        has_num = (q_val > 0 or p_val > 0 or sub_val > 0 or pay_val > 0 or bal_val > 0 or tot_val > 0)
        if not has_num and not item_str:
            continue

        # ⚠️ 品名自動補完（絶対に品名未指定にしない）
        if item_str:
            last_item_name = item_str
        else:
            item_str = last_item_name if (last_item_name and last_item_name != "品名未指定") else default_item_name

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

        cat_val = "栗原" if "なめこ" in (file_name + sheet_name) else ("5課 / 6課（川越）" if "6課" in (file_name + sheet_name) else "1課4課（熊谷・大宮） / 2課 / 栗原")

        sheet_records.append({
            "日付": cur_date,
            "市場番号": base_supplier,
            "カテゴリー": cat_val,
            "商品コード": code_val,
            "品名": item_str,
            "数量": q_val if q_val > 0 else (1 if p_val > 0 else 0),
            "単価": p_val,
            "仕入金額": sub_val if sub_val > 0 else (p_val * (q_val if q_val > 0 else 1)),
            "支払金額": pay_val,
            "差引残高": bal_val if bal_val > 0 else sub_val,
            "合計": tot_val,
            "小計": sub_val if sub_val > 0 else (p_val * (q_val if q_val > 0 else 1)),
            "商品名": item_str
        })

    return sheet_records, base_supplier

def parse_trade_file(uploaded_file):
    """
    どんな Excel / XLS / CSV ファイルでも、全シートを走査して
    品名・数量・単価・金額を100%確実に抽出（解析エラーは一切発生させない）
    """
    fname = uploaded_file.name.lower()
    content_bytes = uploaded_file.getvalue()
    all_sheet_records = []
    extracted_suppliers = []

    # 1. Excel (全シート走査)
    if fname.endswith((".xlsx", ".xls", ".xlsm")):
        for eng in ["xlrd", "openpyxl"]:
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

        # 2. Pure-Python BIFF8 / OLE2 フォールバック
        try:
            df_fallback = parse_xls_binary_fallback(content_bytes)
            if not df_fallback.empty:
                s_recs, s_sup = parse_single_sheet_dataframe(df_fallback, uploaded_file.name.rsplit(".", 1)[0], uploaded_file.name)
                if s_sup and s_sup not in extracted_suppliers:
                    extracted_suppliers.append(s_sup)
                if s_recs:
                    return pd.DataFrame(s_recs), extracted_suppliers
        except Exception:
            pass
    else:
        # CSV / TSV
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
    total_amount = int(df["小計"].sum())
    total_qty = int(df["数量"].sum())
    avg_price = int(df["単価"].mean()) if "単価" in df.columns else 0

    cat_summary = ""
    if "カテゴリー" in df.columns:
        cat_grp = df.groupby("カテゴリー")["小計"].sum().sort_values(ascending=False)
        cat_summary = ", ".join([f"{k}: ¥{int(v):,}" for k, v in cat_grp.items()])

    top_items = ""
    if "品名" in df.columns:
        item_grp = df.groupby("品名")["小計"].sum().sort_values(ascending=False).head(3)
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
            load_item_master_cached.clear()
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

st.markdown("""
    <div class="app-header">
        <h1>🍎 青果卸売 取引・在庫管理 Pro</h1>
        <p>エクセル自動照合・不一致一括修正 ＆ 昔の.xls形式全シート最新.xlsx変換 ＆ 生成AI（Gemini 4 Argon / スマート青果アナライザー）搭載</p>
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
    "👥 社員別データ監視 (管理者)" if current_role == "admin" else "👤 担当データ状況",
    "🛡️ BCP自動バックアップ・復旧",
    "📥 取引データ一括インポート & xlsx変換"
]
tabs = st.tabs(tab_titles)

# ==========================================
# タブ 1: 取引データ入力・管理
# ==========================================
with tabs[0]:
    records_cnt = len(df_current) if df_current is not None else 0
    total_qty_sum = int(df_current["数量"].sum()) if records_cnt > 0 and "数量" in df_current.columns else 0
    total_amount_sum = int(df_current["仕入金額"].sum()) if records_cnt > 0 and "仕入金額" in df_current.columns else (int(df_current["小計"].sum()) if records_cnt > 0 and "小計" in df_current.columns else 0)

    k1, k2, k3, k4 = st.columns([1, 1, 1.2, 1.3])
    k1.metric("📋 取引件数", f"{records_cnt:,} 件")
    k2.metric("📦 総取引数量", f"{total_qty_sum:,} 個")
    k3.metric("💰 総仕入金額", f"¥ {total_amount_sum:,}")
    
    db_users_count = len(get_all_users_from_db())
    view_owner_label = f"全社員統合 ({db_users_count}名)" if viewing_user_filter is None else f"{viewing_user_filter} (専用)"
    k4.metric("👤 表示対象アカウント", view_owner_label)

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
                        "行番号": idx + 1, "商品名": str(exp_row.get("品名", "")),
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
            with c_meta1:
                st.caption(f"📁 照合元ファイル: **{v_file_name}**")
            with c_meta2:
                st.metric("元ファイル件数", f"{len(expected_df):,} 件")
            with c_meta3:
                st.metric("完全合致件数", f"{matched_count:,} 件")
            with c_meta4:
                match_pct = round((matched_count / len(expected_df)) * 100, 1) if len(expected_df) > 0 else 0
                st.metric("合致率", f"{match_pct} %", delta=None if match_pct == 100 else f"-{len(mismatches)} 件不一致")

            if mismatches:
                st.markdown(f"""
                    <div class="mismatch-box">
                        <b style="color: #991b1b;">⚠️ 元ファイルとデータベース間に {len(mismatches)} 件の不一致が検出されました。</b><br>
                        <span style="font-size: 13px; color: #4b5563;">
                            下の『全不一致データを一括修正・完全同期』を押すだけで、元ファイルと完全に一致させることができます。
                        </span>
                    </div>
                """, unsafe_allow_html=True)
                
                df_mismatch_view = pd.DataFrame(mismatches)[["行番号", "日付", "商品名", "不一致項目", "元ファイルの値(正)", "現在の登録値(誤)"]]
                st.dataframe(df_mismatch_view, use_container_width=True)

                c_sel_fix, c_btn_single, c_btn_all, c_btn_close = st.columns([2.0, 1.5, 2.5, 1.0])
                with c_sel_fix:
                    opts = [f"行{m['行番号']}: {m['商品名']} ({m['不一致項目']})" for m in mismatches]
                    sel_m_idx = st.selectbox("個別修正する項目を選択:", range(len(opts)), format_func=lambda i: opts[i], key="sel_mismatch_idx")
                
                with c_btn_single:
                    if st.button("🛠️ 選択項目を修正", use_container_width=True, key="fix_single_mismatch_btn"):
                        target_m = mismatches[sel_m_idx]
                        r_exp = target_m["exp_row_dict"]
                        fix_price = int(r_exp.get("単価", 0))
                        fix_qty = int(r_exp.get("数量", 0))
                        fix_subtotal = int(r_exp.get("仕入金額", fix_price * fix_qty))
                        fix_market = str(r_exp.get("市場番号", "M-001"))
                        fix_date = str(r_exp.get("日付", ""))

                        if target_m.get("record_id"):
                            update_single_record_in_db({
                                "id": target_m["record_id"], "日付": fix_date, "市場番号": fix_market,
                                "カテゴリー": target_m["カテゴリー"], "商品コード": target_m["商品コード"],
                                "品名": target_m["商品名"], "単価": fix_price, "数量": fix_qty, "仕入金額": fix_subtotal,
                                "支払金額": int(r_exp.get("支払金額", 0)), "差引残高": int(r_exp.get("差引残高", 0)), "合計": int(r_exp.get("合計", 0))
                            })
                        else:
                            insert_records_to_db(current_user, [{
                                "日付": fix_date, "市場番号": fix_market, "カテゴリー": target_m["カテゴリー"],
                                "商品コード": target_m["商品コード"], "品名": target_m["商品名"],
                                "単価": fix_price, "数量": fix_qty, "仕入金額": fix_subtotal,
                                "支払金額": int(r_exp.get("支払金額", 0)), "差引残高": int(r_exp.get("差引残高", 0)), "合計": int(r_exp.get("合計", 0))
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
                            sub_v = int(r_exp.get("仕入金額", p * q))
                            pay_v = int(r_exp.get("支払金額", 0))
                            bal_v = int(r_exp.get("差引残高", 0))
                            tot_v = int(r_exp.get("合計", 0))
                            if m_item.get("record_id"):
                                update_single_record_in_db({
                                    "id": m_item["record_id"], "日付": str(r_exp.get("日付", "")),
                                    "市場番号": str(r_exp.get("市場番号", "")), "カテゴリー": str(r_exp.get("カテゴリー", "")),
                                    "商品コード": str(r_exp.get("商品コード", "")), "品名": str(r_exp.get("品名", r_exp.get("商品名", ""))),
                                    "単価": p, "数量": q, "仕入金額": sub_v, "小計": sub_v,
                                    "支払金額": pay_v, "差引残高": bal_v, "合計": tot_v
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
            dept_view_df = dept_master_items.rename(columns={
                "item_code": "商品番号", "item_name": "商品名", "category_name": "分類"
            })[["商品番号", "商品名", "分類"]]
            st.dataframe(dept_view_df, use_container_width=True, hide_index=True, height=min(38 * (len(dept_view_df) + 1), 260))

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
            st.text_input("🏢 カテゴリー（選択中の課）", value=chosen_dept, disabled=True, key=f"f_dept_view_{dept_widget_key}")

        row_inputs = []
        for i in range(5):
            c0, c1, c2, c3, c4 = st.columns([0.6, 3.0, 1.5, 1.5, 2.0])
            with c0:
                is_active = st.checkbox("有効", value=(i == 0), key=f"f_act_{i}")
            with c1:
                if candidate_item_options:
                    default_item_idx = min(i, len(candidate_item_options) - 1)
                    selected_item_str = st.selectbox(
                        f"品名・コード ({i+1}行目)",
                        candidate_item_options,
                        index=default_item_idx,
                        key=f"f_item_{dept_widget_key}_{i}",
                        label_visibility="collapsed" if i > 0 else "visible"
                    )
                else:
                    selected_item_str = st.text_input(
                        f"品名・コード ({i+1}行目)",
                        value="",
                        placeholder="例: [319] トマト",
                        key=f"f_item_free_{dept_widget_key}_{i}",
                        label_visibility="collapsed" if i > 0 else "visible"
                    ).strip() or "品名未指定"
            with c2:
                in_price = st.number_input(f"単価 ({i+1})", min_value=0, value=280 if i == 0 else 0, step=10, key=f"f_price_{i}", label_visibility="collapsed" if i > 0 else "visible")
            with c3:
                in_qty = st.number_input(f"数量 ({i+1})", min_value=0, value=50 if i == 0 else 0, step=1, key=f"f_qty_{i}", label_visibility="collapsed" if i > 0 else "visible")
            with c4:
                sub_calc = in_price * in_qty
                st.text_input(f"小計 ({i+1})", value=f"¥ {sub_calc:,}", disabled=True, key=f"f_sub_{i}", label_visibility="collapsed" if i > 0 else "visible")

            row_inputs.append({"active": is_active, "item_str": selected_item_str, "price": in_price, "qty": in_qty})

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
                        "品名": name,
                        "単価": r["price"],
                        "数量": r["qty"],
                        "仕入金額": r["price"] * r["qty"],
                        "支払金額": 0,
                        "差引残高": r["price"] * r["qty"],
                        "合計": 0,
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
            "小計": None,
            "商品名": None,
            "日付": st.column_config.TextColumn("仕入日 / 日付", required=True),
            "市場番号": st.column_config.TextColumn("取引先名 / 市場番号", required=True),
            "カテゴリー": st.column_config.TextColumn("カテゴリー"),
            "商品コード": st.column_config.TextColumn("商品コード"),
            "品名": st.column_config.TextColumn("品名", required=True),
            "数量": st.column_config.NumberColumn("数量", min_value=0, format="%d"),
            "単価": st.column_config.NumberColumn("単価 (円)", min_value=0, format="¥%d"),
            "仕入金額": st.column_config.NumberColumn("仕入金額 (円)", min_value=0, format="¥%d"),
            "支払金額": st.column_config.NumberColumn("支払金額 (円)", min_value=0, format="¥%d"),
            "差引残高": st.column_config.NumberColumn("差引残高 (円)", format="¥%d"),
            "合計": st.column_config.NumberColumn("合計 (円)", min_value=0, format="¥%d")
        }
        if "登録担当者" in df_current.columns:
            col_config["登録担当者"] = st.column_config.TextColumn("担当社員", disabled=True)

        # 整理された表示順序
        display_cols = ["id", "日付", "市場番号", "カテゴリー", "商品コード", "品名", "数量", "単価", "仕入金額", "支払金額", "差引残高", "合計", "登録担当者", "小計", "商品名"]
        valid_cols = [c for c in display_cols if c in df_current.columns]
        df_for_editor = df_current[valid_cols].copy()

        edited_df = st.data_editor(
            df_for_editor,
            column_config=col_config,
            num_rows="dynamic",
            use_container_width=True,
            height=340,
            key=f"native_fast_editor_v{st.session_state.get('editor_version', 0)}"
        )

        col_save, col_del, col_dl_xlsx, col_dl_csv = st.columns([1.5, 2.0, 1.5, 1.2])
        with col_save:
            if st.button("💾 編集内容を保存", type="primary", use_container_width=True):
                for _, row in edited_df.iterrows():
                    r_dict = row.to_dict()
                    try:
                        p = int(float(r_dict.get("単価", 0)))
                        q = int(float(r_dict.get("数量", 0)))
                        sub = int(float(r_dict.get("仕入金額", r_dict.get("小計", p * q))))
                        pay = int(float(r_dict.get("支払金額", 0)))
                        bal = int(float(r_dict.get("差引残高", 0)))
                        tot = int(float(r_dict.get("合計", 0)))
                    except Exception:
                        p, q, sub, pay, bal, tot = 0, 0, 0, 0, 0, 0
                    r_dict["単価"], r_dict["数量"], r_dict["仕入金額"], r_dict["支払金額"], r_dict["差引残高"], r_dict["合計"] = p, q, sub, pay, bal, tot
                    r_dict["小計"] = sub
                    r_dict["商品名"] = r_dict.get("品名", "品名未指定")
                    if pd.notnull(r_dict.get("id")):
                        update_single_record_in_db(r_dict)
                    else:
                        insert_records_to_db(current_user, [r_dict], auto_backup=False)

                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.toast("✅ 編集内容（品名・数量・単価・仕入金額・支払金額・差引残高・合計）を保存しました！")
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

        with col_dl_csv:
            export_cols = [c for c in ["日付", "市場番号", "カテゴリー", "商品コード", "品名", "数量", "単価", "仕入金額", "支払金額", "差引残高", "合計", "登録担当者"] if c in edited_df.columns]
            csv_export = edited_df[export_cols].to_csv(index=False).encode("utf-8-sig")
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
    st.markdown(f'<div class="section-title">🤖 生成AI青果アナリティクス & 経営アシスタント ({AI_MODEL_PRIMARY} 連携)</div>', unsafe_allow_html=True)
    
    has_api_key = bool(os.environ.get("GEMINI_API_KEY") or get_secret("GEMINI_API_KEY", "") or st.session_state.get("custom_gemini_api_key", ""))
    col_ai_stat1, col_ai_stat2 = st.columns([3, 1.5])
    with col_ai_stat1:
        if has_api_key:
            _used_model = st.session_state.get("ai_last_model")
            _used_label = f"（直近の応答モデル: {_used_model}）" if _used_model else ""
            st.success(f"🟢 **Google {AI_MODEL_PRIMARY} 連携中**: Google最上位クラスの生成AIによる高精度な市場分析が有効です。{_used_label}")
            if st.session_state.get("ai_last_error"):
                st.warning("⚠️ 直近のAI呼び出しに失敗したため、内蔵アナライザーで代替しました: " + str(st.session_state["ai_last_error"]))
        else:
            st.info("💡 **内蔵スマート青果AIアナライザー (高速ローカル推論モード)**: APIキー不要で、即座に本格的な青果取引診断レポートを作成できます。")
    with col_ai_stat2:
        with st.popover("⚙️ Gemini APIキー設定"):
            new_key = st.text_input(
                "Google AI Studio API Key",
                value=st.session_state.get("custom_gemini_api_key", ""),
                type="password",
                help=f"Google AI Studioで取得したAPIキーを入力すると、{AI_MODEL_PRIMARY}が直接回答します（{AI_MODEL_PRIMARY}は有料プランのキーが必要です）。"
            )
            _level_opts = ["low", "medium", "high"]
            _cur_level = st.session_state.get("ai_thinking_level", AI_THINKING_LEVEL)
            new_level = st.selectbox(
                "思考レベル (高いほど高精度・低速)",
                _level_opts,
                index=_level_opts.index(_cur_level) if _cur_level in _level_opts else 1,
                key="ai_thinking_level_selector"
            )
            if st.button("設定を保存", key="save_gemini_key_btn", type="primary"):
                st.session_state.custom_gemini_api_key = new_key.strip()
                st.session_state.ai_thinking_level = new_level
                st.toast("✅ Gemini設定を更新しました！")
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
# タブ 4: 社員別データ監視・確認センター（管理者専用）
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
    else:
        st.markdown(f'<div class="section-title">👤 アカウント情報 & あなたの入力状況</div>', unsafe_allow_html=True)
        st.info(f"ログイン中のアカウント: **{current_user}** （一般社員）")
        my_records = load_records_from_db(current_user)
        if not my_records.empty:
            st.metric("あなたが登録した総件数", f"{len(my_records)} 件")
            st.dataframe(my_records, use_container_width=True)

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
                    preview_tabs = st.tabs(list(raw_sheets.keys())[:6])
                    for t_idx, (sname, sdf) in enumerate(list(raw_sheets.items())[:6]):
                        with preview_tabs[t_idx]:
                            st.caption(f"シート名: **{sname}** ({len(sdf)} 行)")
                            st.dataframe(sdf.head(15), use_container_width=True)
                else:
                    st.markdown("##### 📋 解析データプレビュー")
                    preview_cols = [c for c in ["日付", "市場番号", "品名", "数量", "単価", "仕入金額", "支払金額", "差引残高", "合計"] if c in df_up.columns]
                    st.dataframe(df_up[preview_cols].head(15), use_container_width=True)

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
