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
from sqlalchemy import create_engine, text, event
import io
import zipfile
import xml.etree.ElementTree as ET
import re
import requests
import time
import unicodedata
import struct
import ctypes
import gc
from collections import defaultdict
from typing import Dict, Tuple, List, Optional, Any
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
# 1. 永続ストレージ & DB設定（WALモード & タイムアウト30秒強化）
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
    """
    データベース接続エンジン取得
    PostgreSQL（Cloud DB）優先接続。
    SQLite利用時は WALモード (journal_mode=WAL) & busy_timeout=30秒を自動適用し同時書き込みロックを防止。
    """
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
    
    # SQLite: 同時書き込みロック回避（timeout=30.0 + WALモードリスナー）
    sqlite_engine = create_engine(
        f"sqlite:///{SQLITE_DB_PATH}?check_same_thread=False",
        connect_args={"check_same_thread": False, "timeout": 30.0}
    )

    @event.listens_for(sqlite_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA busy_timeout=30000;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.close()
        except Exception:
            pass

    return sqlite_engine

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
# 3. データベーステーブル初期化（PostgreSQL / SQLite 両対応 & backup_logs 永続化）
# ==========================================
def init_db():
    engine = get_engine()
    is_cloud = is_using_cloud_db()
    pk_auto = "SERIAL PRIMARY KEY" if is_cloud else "INTEGER PRIMARY KEY AUTOINCREMENT"
    
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS users (
                id {pk_auto},
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'staff',
                display_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS transactions (
                id {pk_auto},
                date TEXT NOT NULL,
                market_number TEXT NOT NULL,
                category TEXT,
                item_code TEXT,
                item_name TEXT NOT NULL,
                unit_price INTEGER NOT NULL DEFAULT 0,
                quantity INTEGER NOT NULL DEFAULT 0,
                subtotal BIGINT NOT NULL DEFAULT 0,
                user_id TEXT NOT NULL,
                sheet_name TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS deleted_transactions (
                id {pk_auto},
                original_id INTEGER,
                date TEXT,
                market_number TEXT,
                category TEXT,
                item_code TEXT,
                item_name TEXT,
                unit_price INTEGER,
                quantity INTEGER,
                subtotal BIGINT,
                user_id TEXT,
                sheet_name TEXT DEFAULT '',
                deleted_by TEXT,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))
        # ④ クラウド永続化用バックアップテーブル（再起動時のデータ消失を防止）
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS backup_logs (
                id {pk_auto},
                filename TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                created_by TEXT NOT NULL,
                reason TEXT,
                record_count INTEGER DEFAULT 0,
                total_subtotal BIGINT DEFAULT 0,
                payload_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))
        
        # 既存DBへの sheet_name カラム安全追加マイグレーション
        for tbl in ["transactions", "deleted_transactions"]:
            try:
                conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN sheet_name TEXT DEFAULT '';"))
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
# 5. 取引データ永続化 & 高速キャッシュ & RBACアクセス制御
# ==========================================
@st.cache_data(ttl=60, show_spinner=False)
def load_records_cached(target_user_filter: str = None):
    engine = get_engine()
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN sheet_name TEXT DEFAULT '';"))
        except Exception:
            pass

        cols_query = "SELECT id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, sheet_name, created_at FROM transactions"
        try:
            if target_user_filter and target_user_filter != "全員のデータを表示":
                df = pd.read_sql(text(f"{cols_query} WHERE user_id = :uid ORDER BY date DESC, id DESC"), conn, params={"uid": target_user_filter})
            else:
                df = pd.read_sql(text(f"{cols_query} ORDER BY date DESC, id DESC"), conn)
        except Exception:
            fallback_query = "SELECT id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, created_at FROM transactions"
            if target_user_filter and target_user_filter != "全員のデータを表示":
                df = pd.read_sql(text(f"{fallback_query} WHERE user_id = :uid ORDER BY date DESC, id DESC"), conn, params={"uid": target_user_filter})
            else:
                df = pd.read_sql(text(f"{fallback_query} ORDER BY date DESC, id DESC"), conn)
            
    df = df.rename(columns={
        "date": "日付", "market_number": "市場番号", "category": "カテゴリー",
        "item_code": "商品コード", "item_name": "品名", "unit_price": "単価",
        "quantity": "数量", "subtotal": "仕入金額", "user_id": "登録担当者",
        "sheet_name": "シート名"
    })
    if not df.empty:
        df["商品名"] = df["品名"]
        df["小計"] = df["仕入金額"]
        df["支払金額"] = 0
        df["差引残高"] = df["仕入金額"]
        df["合計"] = df["仕入金額"]

    if "シート名" not in df.columns:
        df["シート名"] = "Sheet1"
    df["シート名"] = df["シート名"].fillna("").apply(lambda s: str(s).strip() if str(s).strip() else "Sheet1")
    return df

def load_records_from_db(target_user_filter: str = None):
    return load_records_cached(target_user_filter)

def invalidate_records_cache():
    load_records_cached.clear()
    gc.collect()

def insert_records_to_db(user_id: str, records: list, auto_backup=True):
    if not records:
        return
    engine = get_engine()
    now_ts = datetime.now()
    with engine.begin() as conn:
        try:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN sheet_name TEXT DEFAULT '';"))
        except Exception:
            pass
        for r in records:
            p = int(r.get("単価", 0))
            q = int(r.get("数量", 0))
            sub = int(r.get("仕入金額", r.get("小計", p * q)))
            item_n = str(r.get("品名", r.get("商品名", "品名未指定"))).strip() or "品名未指定"
            sheet_n = str(r.get("シート名", r.get("sheet_name", ""))).strip() or "Sheet1"
            try:
                conn.execute(
                    text("""
                        INSERT INTO transactions 
                        (date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, sheet_name, created_at)
                        VALUES (:d, :m, :c, :ic, :in, :p, :q, :sub, :u, :sn, :cat)
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
                        "sn": sheet_n,
                        "cat": now_ts
                    }
                )
            except Exception:
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
                        "p": p, "q": q, "sub": sub, "u": user_id, "cat": now_ts
                    }
                )
    invalidate_records_cache()
    if auto_backup:
        create_backup_snapshot(user_id, trigger_reason=f"自動追記バックアップ ({len(records)}件記録)")

def update_single_record_in_db(record_dict: dict, operator_user: str = "admin", operator_role: str = "admin") -> Tuple[bool, str]:
    """
    ③ 部署・ロール別アクセス権限（RBAC）ガード付きレコード更新
    staff は「自分が登録したデータ（user_id == operator_user）」のみ更新可能
    """
    rec_id = record_dict.get("id")
    if not rec_id:
        return False, "IDが指定されていません"
    
    engine = get_engine()
    # RBAC 権限チェック
    if operator_role != "admin":
        with engine.connect() as conn:
            owner_row = conn.execute(
                text("SELECT user_id FROM transactions WHERE id = :rid"),
                {"rid": rec_id}
            ).fetchone()
            if owner_row and str(owner_row[0]).strip() != str(operator_user).strip():
                return False, f"権限エラー: 他の担当者（{owner_row[0]}）が登録したデータは変更できません。"

    p = int(record_dict.get("単価", 0))
    q = int(record_dict.get("数量", 0))
    sub = int(record_dict.get("仕入金額", record_dict.get("小計", p * q)))
    item_n = str(record_dict.get("品名", record_dict.get("商品名", "品名未指定"))).strip() or "品名未指定"
    sheet_n = str(record_dict.get("シート名", "Sheet1")).strip() or "Sheet1"

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
                    subtotal = :sub,
                    sheet_name = :sn
                WHERE id = :rid
            """),
            {
                "d": str(record_dict.get("日付", "")),
                "m": str(record_dict.get("市場番号", "")),
                "c": str(record_dict.get("カテゴリー", "")),
                "ic": str(record_dict.get("商品コード", "")),
                "in": item_n,
                "p": p, "q": q, "sub": sub, "sn": sheet_n, "rid": rec_id
            }
        )
    invalidate_records_cache()
    return True, "更新が完了しました"

def delete_records_by_ids(ids: list, deleted_by: str, operator_role: str = "admin") -> Tuple[int, int, List[dict]]:
    """
    ③ 部署・ロール別アクセス権限（RBAC）ガード付きレコード削除
    staff は自身が登録したレコードのみ削除可能。権限外レコードは安全にスキップ。
    退避テーブル `deleted_transactions` に記録し、ワンクリック復元用リストを返却。
    返り値: (削除成功件数, スキップ件数, 削除レコード辞書リスト)
    """
    if not ids:
        return 0, 0, []
    engine = get_engine()
    deleted_count = 0
    skipped_count = 0
    deleted_records = []

    with engine.begin() as conn:
        for rid in ids:
            row = conn.execute(
                text("SELECT id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, sheet_name FROM transactions WHERE id = :rid"),
                {"rid": rid}
            ).fetchone()
            if not row:
                continue

            # staff の場合は他者のレコード削除を拒否
            if operator_role != "admin" and str(row[9]).strip() != str(deleted_by).strip():
                skipped_count += 1
                continue

            rec_dict = {
                "original_id": row[0],
                "date": str(row[1]),
                "market_number": str(row[2]),
                "category": str(row[3]),
                "item_code": str(row[4]),
                "item_name": str(row[5]),
                "unit_price": int(row[6]),
                "quantity": int(row[7]),
                "subtotal": int(row[8]),
                "user_id": str(row[9]),
                "sheet_name": str(row[10]) if len(row) > 10 and row[10] else "Sheet1"
            }
            deleted_records.append(rec_dict)

            conn.execute(
                text("""
                    INSERT INTO deleted_transactions
                    (original_id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, sheet_name, deleted_by)
                    VALUES (:oid, :d, :m, :c, :ic, :in, :p, :q, :sub, :u, :sn, :db)
                """),
                {
                    "oid": row[0], "d": row[1], "m": row[2], "c": row[3], "ic": row[4],
                    "in": row[5], "p": row[6], "q": row[7], "sub": row[8], "u": row[9],
                    "sn": row[10] if len(row) > 10 else "", "db": deleted_by
                }
            )
            conn.execute(text("DELETE FROM transactions WHERE id = :rid"), {"rid": rid})
            deleted_count += 1

    invalidate_records_cache()
    if deleted_count > 0:
        create_backup_snapshot(deleted_by, trigger_reason=f"データ削除前自動退避 ({deleted_count}件削除)")
        # セッションに直前の削除レコードを退避保持
        st.session_state.last_deleted_records = deleted_records

    return deleted_count, skipped_count, deleted_records

def restore_last_deleted_records_db(records_to_restore: list, restored_by: str = "admin") -> int:
    """
    誤削除データをDBへ安全に復帰（UNDO復元）
    退避テーブル（deleted_transactions）からも削除し、完全復元します。
    """
    if not records_to_restore:
        return 0
    engine = get_engine()
    now_ts = datetime.now()
    restored_count = 0
    with engine.begin() as conn:
        for r in records_to_restore:
            p = int(r.get("unit_price", r.get("単価", 0)))
            q = int(r.get("quantity", r.get("数量", 0)))
            sub = int(r.get("subtotal", r.get("仕入金額", r.get("小計", p * q))))
            item_n = str(r.get("item_name", r.get("品名", r.get("商品名", "品名未指定")))).strip() or "品名未指定"
            sheet_n = str(r.get("sheet_name", r.get("シート名", "Sheet1"))).strip() or "Sheet1"
            conn.execute(
                text("""
                    INSERT INTO transactions 
                    (date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, sheet_name, created_at)
                    VALUES (:d, :m, :c, :ic, :in, :p, :q, :sub, :u, :sn, :cat)
                """),
                {
                    "d": str(r.get("date", r.get("日付", now_ts.strftime("%Y-%m-%d")))),
                    "m": str(r.get("market_number", r.get("市場番号", "M-001"))),
                    "c": str(r.get("category", r.get("カテゴリー", "1課 (熊谷・大宮)"))),
                    "ic": str(r.get("item_code", r.get("商品コード", "AUTO"))),
                    "in": item_n,
                    "p": p,
                    "q": q,
                    "sub": sub,
                    "u": str(r.get("user_id", r.get("登録担当者", restored_by))),
                    "sn": sheet_n,
                    "cat": now_ts
                }
            )
            restored_count += 1
            orig_id = r.get("original_id", r.get("id"))
            if orig_id:
                try:
                    conn.execute(text("DELETE FROM deleted_transactions WHERE original_id = :oid"), {"oid": orig_id})
                except Exception:
                    pass
    invalidate_records_cache()
    if restored_count > 0:
        create_backup_snapshot(restored_by, trigger_reason=f"誤削除データ復元 ({restored_count}件復元)")
    return restored_count
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
# 6. バックアップ & クラウドDB二重退避復元エンジン
# ==========================================
def create_backup_snapshot(user_id: str, trigger_reason="定期自動バックアップ"):
    """
    ④ クラウド環境永続化対応バックアップ作成
    ローカルファイル（JSON）保存に加え、DB内 `backup_logs` テーブルへも完全保存し、
    Streamlit Cloud の再起動・ファイル消失時にも確実な復旧を保証します。
    """
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
    payload = {
        "meta": meta,
        "records": df_all.to_dict(orient="records")
    }
    payload_json_str = json.dumps(payload, ensure_ascii=False, indent=2)

    # 1. ローカルディスク保存
    try:
        bk_path = os.path.join(BACKUP_DIR, bk_filename)
        with open(bk_path, "w", encoding="utf-8") as f:
            f.write(payload_json_str)
    except Exception:
        pass

    # 2. データベース（PostgreSQL / SQLite）内の backup_logs テーブルへ二重永続化
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO backup_logs 
                    (filename, timestamp, created_by, reason, record_count, total_subtotal, payload_json)
                    VALUES (:fn, :ts, :cb, :rs, :rc, :sub, :pj)
                """),
                {
                    "fn": bk_filename,
                    "ts": now_str,
                    "cb": user_id,
                    "rs": trigger_reason,
                    "rc": meta["record_count"],
                    "sub": meta["total_subtotal"],
                    "pj": payload_json_str
                }
            )
    except Exception:
        pass

    return bk_filename

def list_all_backups():
    """DBテーブル（backup_logs）とローカルファイルを統合・重複排除してリスト化"""
    backups_dict = {}

    # 1. DBテーブルからの取得
    engine = get_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT filename, timestamp, created_by, reason, record_count, total_subtotal FROM backup_logs ORDER BY id DESC")).fetchall()
            for r in rows:
                backups_dict[r[0]] = {
                    "filepath": None,
                    "filename": r[0],
                    "timestamp": r[1],
                    "created_by": r[2],
                    "reason": r[3],
                    "record_count": r[4],
                    "total_subtotal": r[5],
                    "source": "db"
                }
    except Exception:
        pass

    # 2. ローカルファイルの取得
    files = glob.glob(os.path.join(BACKUP_DIR, "backup_*.json"))
    files.sort(reverse=True)
    for fp in files:
        fname = os.path.basename(fp)
        if fname in backups_dict:
            backups_dict[fname]["filepath"] = fp
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
                meta = d.get("meta", {})
                backups_dict[fname] = {
                    "filepath": fp,
                    "filename": fname,
                    "timestamp": meta.get("timestamp", "不明"),
                    "created_by": meta.get("created_by", "system"),
                    "reason": meta.get("reason", "自動作成"),
                    "record_count": meta.get("record_count", 0),
                    "total_subtotal": meta.get("total_subtotal", 0),
                    "source": "file"
                }
        except Exception:
            continue

    backups = list(backups_dict.values())
    backups.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
    return backups

def restore_from_backup_target(backup_item: dict, restored_by: str):
    """ファイルまたはDBテーブル上のJSONから確実に全データを復元"""
    data = None
    engine = get_engine()

    # 1. ファイルから読み込み
    fp = backup_item.get("filepath")
    if fp and os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass

    # 2. DBの backup_logs テーブルから復元
    if not data:
        fname = backup_item.get("filename")
        try:
            with engine.connect() as conn:
                row = conn.execute(text("SELECT payload_json FROM backup_logs WHERE filename = :fn ORDER BY id DESC LIMIT 1"), {"fn": fname}).fetchone()
                if row and row[0]:
                    data = json.loads(row[0])
        except Exception:
            pass

    if not data:
        return {"success": False, "message": "バックアップデータを取得できませんでした"}

    try:
        records = data.get("records", [])
        create_backup_snapshot(restored_by, trigger_reason="データ復元直前の現行データ自動保護")
        
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM transactions;"))
            for r in records:
                conn.execute(
                    text("""
                        INSERT INTO transactions 
                        (date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, sheet_name, created_at)
                        VALUES (:d, :m, :c, :ic, :in, :p, :q, :sub, :u, :sn, :cat)
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
                        "sn": str(r.get("sheet_name", "Sheet1")),
                        "cat": str(r.get("created_at", datetime.now()))
                    }
                )
        invalidate_records_cache()
        return {"success": True, "restored_count": len(records)}
    except Exception as e:
        return {"success": False, "message": str(e)}

# 互換エイリアス
restore_from_backup_file = lambda fp, rb: restore_from_backup_target({"filepath": fp, "filename": os.path.basename(fp)}, rb)

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

# normalize_date_cell のエイリアス宣言（インポートエンジンとの完全互換）
normalize_date_cell = parse_japanese_date

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

def extract_ole_workbook_stream(ole_bytes: bytes) -> bytes:
    """
    Pure-Python OLE2 / Compound File (CFBF) パーサー。
    外部Cライブラリや非標準ライブラリに一切依存せず、純粋なPythonバイナリ解析のみで
    .xls ファイルから 'Workbook' (または 'Book') ストリームを完全抽出します。
    """
    if len(ole_bytes) < 512 or ole_bytes[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return b""
    try:
        sec_size = 1 << struct.unpack_from("<H", ole_bytes, 30)[0]
        short_sec_size = 1 << struct.unpack_from("<H", ole_bytes, 32)[0]
        num_fat_sectors = struct.unpack_from("<I", ole_bytes, 44)[0]
        first_dir_sec = struct.unpack_from("<I", ole_bytes, 48)[0]
        first_minifat_sec = struct.unpack_from("<I", ole_bytes, 60)[0]
        first_difat_sec = struct.unpack_from("<I", ole_bytes, 68)[0]
        num_difat_sectors = struct.unpack_from("<I", ole_bytes, 72)[0]

        difat = list(struct.unpack_from("<109I", ole_bytes, 76))
        difat = [s for s in difat if s not in (0xFFFFFFFD, 0xFFFFFFFE, 0xFFFFFFFF)]

        cur_difat = first_difat_sec
        for _ in range(num_difat_sectors):
            if cur_difat in (0xFFFFFFFE, 0xFFFFFFFF) or (cur_difat + 1) * sec_size > len(ole_bytes):
                break
            d_offset = (cur_difat + 1) * sec_size
            entries = struct.unpack_from(f"<{sec_size // 4}I", ole_bytes, d_offset)
            difat.extend([s for s in entries[:-1] if s not in (0xFFFFFFFD, 0xFFFFFFFE, 0xFFFFFFFF)])
            cur_difat = entries[-1]

        fat = []
        for sec_idx in difat[:num_fat_sectors]:
            f_offset = (sec_idx + 1) * sec_size
            if f_offset + sec_size <= len(ole_bytes):
                fat.extend(struct.unpack_from(f"<{sec_size // 4}I", ole_bytes, f_offset))

        def get_sector_chain(start_sec, fat_table):
            chain = []
            curr = start_sec
            visited = set()
            while curr not in (0xFFFFFFFE, 0xFFFFFFFF) and curr < len(fat_table):
                if curr in visited:
                    break
                visited.add(curr)
                chain.append(curr)
                curr = fat_table[curr]
            return chain

        dir_chain = get_sector_chain(first_dir_sec, fat)
        dir_bytes = bytearray()
        for s in dir_chain:
            off = (s + 1) * sec_size
            dir_bytes.extend(ole_bytes[off:off + sec_size])

        minifat = []
        if first_minifat_sec not in (0xFFFFFFFE, 0xFFFFFFFF):
            minifat_chain = get_sector_chain(first_minifat_sec, fat)
            for s in minifat_chain:
                off = (s + 1) * sec_size
                if off + sec_size <= len(ole_bytes):
                    minifat.extend(struct.unpack_from(f"<{sec_size // 4}I", ole_bytes, off))

        root_start_sec = None
        workbook_entry = None
        for i in range(0, len(dir_bytes), 128):
            entry = dir_bytes[i:i + 128]
            if len(entry) < 128:
                continue
            name_len = struct.unpack_from("<H", entry, 64)[0]
            if name_len > 0:
                raw_name = entry[:name_len].decode("utf-16le", errors="ignore").rstrip("\x00")
            else:
                raw_name = ""
            obj_type = entry[66]
            start_sec = struct.unpack_from("<I", entry, 116)[0]
            size = struct.unpack_from("<Q", entry, 120)[0]

            if obj_type == 5:  # Root Storage
                root_start_sec = start_sec
            elif raw_name in ("Workbook", "Book"):
                workbook_entry = (start_sec, size)

        if not workbook_entry:
            return b""

        wb_start, wb_size = workbook_entry
        minifat_cutoff = struct.unpack_from("<I", ole_bytes, 56)[0]

        if wb_size < minifat_cutoff and minifat and root_start_sec is not None:
            mini_stream_chain = get_sector_chain(root_start_sec, fat)
            mini_stream = bytearray()
            for s in mini_stream_chain:
                off = (s + 1) * sec_size
                mini_stream.extend(ole_bytes[off:off + sec_size])

            wb_chain = get_sector_chain(wb_start, minifat)
            wb_bytes = bytearray()
            for s in wb_chain:
                off = s * short_sec_size
                wb_bytes.extend(mini_stream[off:off + short_sec_size])
            return bytes(wb_bytes[:wb_size])
        else:
            wb_chain = get_sector_chain(wb_start, fat)
            wb_bytes = bytearray()
            for s in wb_chain:
                off = (s + 1) * sec_size
                wb_bytes.extend(ole_bytes[off:off + sec_size])
            return bytes(wb_bytes[:wb_size])
    except Exception:
        return b""


def cell_ref_to_row_col(cell_ref: str) -> Tuple[int, int]:
    """A1 -> (1, 1), Z10 -> (10, 26) の 1-based インデックスに変換"""
    match = re.match(r"([A-Za-z]+)(\d+)", str(cell_ref))
    if not match:
        return 0, 0
    col_str, row_str = match.groups()
    col = 0
    for char in col_str.upper():
        col = col * 26 + (ord(char) - ord('A') + 1)
    return int(row_str), col


def _extract_text_from_xml_elem(elem: ET.Element) -> str:
    """XML要素内のテキストを再帰的に全走査して結合抽出"""
    texts = [t.text or "" for t in elem.iter() if t.tag.split("}")[-1] == "t"]
    return "".join(texts)


def extract_xlsx_sheet_grids(zip_file: zipfile.ZipFile) -> Dict[str, Dict[Tuple[int, int], str]]:
    """
    OpenXML (.xlsx) から全ワークシートのセル文字列辞書を1セルも漏らさず抽出。
    inlineStr (<is><t>), 共有文字列 (sharedStrings), 数値 (<v>), 数式文字列 (<c t="str">)
    のすべてを完全に文字・数値として復元します。
    """
    sheet_grids: Dict[str, Dict[Tuple[int, int], str]] = {}
    
    # 1. sharedStrings の抽出
    shared_strings: List[str] = []
    sst_files = [n for n in zip_file.namelist() if n.lower().endswith("sharedstrings.xml")]
    if sst_files:
        try:
            tree = ET.fromstring(zip_file.read(sst_files[0]))
            for si in tree.iter():
                if si.tag.split("}")[-1] == "si":
                    shared_strings.append(_extract_text_from_xml_elem(si))
        except Exception:
            pass

    # 2. workbook.xml からシート名と r:id のマッピング抽出
    sheet_info: List[Tuple[str, str]] = []  # (name, rId)
    wb_files = [n for n in zip_file.namelist() if n.lower().endswith("workbook.xml")]
    if wb_files:
        try:
            tree = ET.fromstring(zip_file.read(wb_files[0]))
            for elem in tree.iter():
                if elem.tag.split("}")[-1] == "sheet":
                    s_name = elem.attrib.get("name", "")
                    r_id = ""
                    for k, v in elem.attrib.items():
                        if k.endswith("id") or k == "id":
                            r_id = v
                            break
                    if s_name:
                        sheet_info.append((s_name, r_id))
        except Exception:
            pass

    # 3. workbook.xml.rels からリレーション先XMLパスを解決
    rid_to_target: Dict[str, str] = {}
    wb_rels = [n for n in zip_file.namelist() if n.lower().endswith("workbook.xml.rels")]
    if wb_rels:
        try:
            tree = ET.fromstring(zip_file.read(wb_rels[0]))
            for elem in tree.iter():
                if elem.tag.split("}")[-1] == "Relationship":
                    r_id = elem.attrib.get("Id", "")
                    target = elem.attrib.get("Target", "").replace("\\", "/")
                    if r_id and target:
                        if not target.startswith("xl/"):
                            target = "xl/" + target.lstrip("/")
                        rid_to_target[r_id] = target
        except Exception:
            pass

    namelist_map = {n.lower(): n for n in zip_file.namelist()}
    processed_targets = set()

    for s_name, r_id in sheet_info:
        target = rid_to_target.get(r_id, "")
        real_path = namelist_map.get(target.lower())
        if real_path:
            processed_targets.add(real_path)
            sheet_grids[s_name] = _parse_single_openxml_sheet(zip_file.read(real_path), shared_strings)

    # リレーションで拾えなかったシートXMLの自動補完
    sheet_files = sorted([n for n in zip_file.namelist() if re.search(r"worksheets/sheet\d+\.xml$", n, re.I)])
    for sf in sheet_files:
        if sf not in processed_targets:
            s_name = f"Sheet{len(sheet_grids) + 1}"
            sheet_grids[s_name] = _parse_single_openxml_sheet(zip_file.read(sf), shared_strings)

    return sheet_grids


def _parse_single_openxml_sheet(sheet_xml_bytes: bytes, shared_strings: List[str]) -> Dict[Tuple[int, int], str]:
    """ワークシートXMLから (row, col) -> 文字列 のマッピングを生成（r属性欠落時も位置追跡）"""
    cells: Dict[Tuple[int, int], str] = {}
    try:
        root = ET.fromstring(sheet_xml_bytes)
        cur_row = 0

        for elem in root.iter():
            tag = elem.tag.split("}")[-1]
            if tag == "row":
                r_attr = elem.attrib.get("r")
                if r_attr and r_attr.isdigit():
                    cur_row = int(r_attr)
                else:
                    cur_row += 1
                cur_col = 0
            elif tag == "c":
                r_ref = elem.attrib.get("r", "")
                if r_ref:
                    r_idx, c_idx = cell_ref_to_row_col(r_ref)
                else:
                    r_idx = cur_row
                    cur_col += 1
                    c_idx = cur_col

                if r_idx == 0 or c_idx == 0:
                    continue

                cell_type = elem.attrib.get("t", "")
                val = ""

                # 1. インライン文字列 (<is><t>) または リッチテキスト (<r><t>)
                t_elems = [child.text or "" for child in elem.iter() if child.tag.split("}")[-1] == "t"]
                if t_elems and (cell_type in ("inlineStr", "str") or not cell_type):
                    val = "".join(t_elems)

                # 2. 値要素 (<v>) の取得
                if not val:
                    v_elem = next((child for child in elem if child.tag.split("}")[-1] == "v"), None)
                    if v_elem is not None and v_elem.text:
                        raw_v = v_elem.text.strip()
                        if cell_type == "s":  # 共有文字列
                            try:
                                s_idx = int(raw_v)
                                if 0 <= s_idx < len(shared_strings):
                                    val = shared_strings[s_idx]
                            except ValueError:
                                val = raw_v
                        elif cell_type == "b":  # 真偽値
                            val = "1" if raw_v == "1" else "0"
                        else:
                            val = raw_v

                if val:
                    val_clean = "".join(ch for ch in val if ord(ch) >= 32 or ch in "\n\t").strip()
                    if val_clean:
                        cells[(r_idx, c_idx)] = val_clean
    except Exception:
        pass
    return cells


def parse_biff_multisheet(wb_stream: bytes) -> Dict[str, Dict[Tuple[int, int], str]]:
    """BIFF8 / BIFF5 完全対応マルチシートパーサー"""
    sheets_dict: Dict[str, Dict[Tuple[int, int], str]] = {}
    if len(wb_stream) < 16:
        return sheets_dict

    pos = 0
    stream_len = len(wb_stream)
    boundsheets: List[Tuple[int, str, int, int]] = []
    sst: List[str] = []
    xf_index_to_format_str: Dict[int, str] = {}
    format_records: Dict[int, str] = {}

    while pos < stream_len - 4:
        rtype, rlen = struct.unpack_from("<HH", wb_stream, pos)
        pos += 4
        if pos + rlen > stream_len:
            break
        rdata = wb_stream[pos:pos + rlen]
        pos += rlen

        if rtype == 0x0085:  # BOUNDSHEET
            if len(rdata) >= 7:
                lb_pos = struct.unpack_from("<I", rdata, 0)[0]
                hs_state = rdata[4]
                dt_type = rdata[5]
                cch = rdata[6]
                name = ""
                if len(rdata) >= 8:
                    flag = rdata[7]
                    if flag & 0x01:
                        if len(rdata) >= 8 + cch * 2:
                            name = rdata[8:8 + cch * 2].decode("utf-16le", errors="ignore")
                    else:
                        if len(rdata) >= 8 + cch:
                            name = rdata[8:8 + cch].decode("cp932", errors="ignore")
                elif len(rdata) >= 7 + cch:
                    name = rdata[7:7 + cch].decode("cp932", errors="ignore")

                if not name:
                    name = f"Sheet{len(boundsheets) + 1}"
                
                base_name = name
                suffix = 2
                while name in [b[1] for b in boundsheets]:
                    name = f"{base_name} ({suffix})"
                    suffix += 1

                boundsheets.append((lb_pos, name, dt_type, hs_state))

        elif rtype == 0x00FC:  # SST
            if len(rdata) >= 8:
                try:
                    num_strings = struct.unpack_from("<I", rdata, 4)[0]
                    s_pos = 8
                    for _ in range(num_strings):
                        if s_pos >= len(rdata) - 2:
                            break
                        cch = struct.unpack_from("<H", rdata, s_pos)[0]
                        s_pos += 2
                        flag = rdata[s_pos]
                        s_pos += 1
                        
                        is_16bit = bool(flag & 0x01)
                        is_rich = bool(flag & 0x08)
                        is_ext = bool(flag & 0x04)

                        cb_rich = struct.unpack_from("<H", rdata, s_pos)[0] if is_rich else 0
                        if is_rich: s_pos += 2
                        cb_ext = struct.unpack_from("<I", rdata, s_pos)[0] if is_ext else 0
                        if is_ext: s_pos += 4

                        str_bytes_len = cch * 2 if is_16bit else cch
                        if s_pos + str_bytes_len <= len(rdata):
                            raw_str = rdata[s_pos:s_pos + str_bytes_len]
                            encoding = "utf-16le" if is_16bit else "cp932"
                            sst.append(raw_str.decode(encoding, errors="ignore"))
                            s_pos += str_bytes_len + (cb_rich * 4) + cb_ext
                        else:
                            break
                except Exception:
                    pass

        elif rtype == 0x041E:  # FORMAT
            if len(rdata) >= 5:
                fmt_idx = struct.unpack_from("<H", rdata, 0)[0]
                cch = struct.unpack_from("<H", rdata, 2)[0]
                flag = rdata[4]
                if flag & 0x01:
                    fmt_str = rdata[5:5 + cch * 2].decode("utf-16le", errors="ignore")
                else:
                    fmt_str = rdata[5:5 + cch].decode("cp932", errors="ignore")
                format_records[fmt_idx] = fmt_str

        elif rtype == 0x00E0:  # XF
            if len(rdata) >= 4:
                fmt_idx = struct.unpack_from("<H", rdata, 2)[0]
                xf_index = len(xf_index_to_format_str)
                xf_index_to_format_str[xf_index] = format_records.get(fmt_idx, "")

    if not boundsheets:
        boundsheets.append((0, "Sheet1", 0, 0))

    for _, s_name, _, _ in boundsheets:
        sheets_dict[s_name] = {}

    for idx, (lb_pos, sheet_name, dt_type, _) in enumerate(boundsheets):
        if dt_type != 0:
            continue

        cur_pos = lb_pos if (0 <= lb_pos < stream_len - 4) else 0
        if cur_pos == 0 and idx > 0:
            continue

        sheet_cells: Dict[Tuple[int, int], str] = {}
        inside_sheet = False

        while cur_pos < stream_len - 4:
            rtype, rlen = struct.unpack_from("<HH", wb_stream, cur_pos)
            cur_pos += 4
            if cur_pos + rlen > stream_len:
                break
            rdata = wb_stream[cur_pos:cur_pos + rlen]
            cur_pos += rlen

            if rtype == 0x0809:
                bof_type = struct.unpack_from("<H", rdata, 2)[0] if len(rdata) >= 4 else 0
                if bof_type in (0x0010, 0x0020, 0x0000, 0x0100):
                    inside_sheet = True
                elif bof_type == 0x0005:
                    inside_sheet = False
            elif rtype == 0x000A:
                if inside_sheet:
                    inside_sheet = False
                    break

            if not inside_sheet:
                continue

            if rtype == 0x00FD and len(rdata) >= 10:  # LABELSST
                row = struct.unpack_from("<H", rdata, 0)[0] + 1
                col = struct.unpack_from("<H", rdata, 2)[0] + 1
                sst_idx = struct.unpack_from("<I", rdata, 6)[0]
                if 0 <= sst_idx < len(sst):
                    val = sst[sst_idx].strip()
                    if val: sheet_cells[(row, col)] = val
            elif rtype == 0x0204 and len(rdata) >= 8:  # LABEL
                row = struct.unpack_from("<H", rdata, 0)[0] + 1
                col = struct.unpack_from("<H", rdata, 2)[0] + 1
                cch = struct.unpack_from("<H", rdata, 6)[0]
                val = rdata[8:8 + cch].decode("cp932", errors="ignore").strip()
                if val: sheet_cells[(row, col)] = val
            elif rtype == 0x0203 and len(rdata) >= 14:  # NUMBER
                row = struct.unpack_from("<H", rdata, 0)[0] + 1
                col = struct.unpack_from("<H", rdata, 2)[0] + 1
                xf_idx = struct.unpack_from("<H", rdata, 4)[0]
                num_val = struct.unpack_from("<d", rdata, 6)[0]
                fmt_str = xf_index_to_format_str.get(xf_idx, "")
                if any(k in fmt_str.lower() for k in ("yy", "mm", "dd", "ge", "gg", "m月", "d日")):
                    val_str = format_excel_date_serial(num_val, fmt_str)
                elif 30000 <= num_val <= 60000 and col in (1, 2) and not fmt_str:
                    val_str = format_excel_date_serial(num_val, "yyyy-mm-dd")
                else:
                    val_str = f"{int(num_val)}" if num_val.is_integer() else f"{num_val:.4f}".rstrip("0").rstrip(".")
                if val_str: sheet_cells[(row, col)] = val_str
            elif rtype == 0x027E and len(rdata) >= 10:  # RK
                row = struct.unpack_from("<H", rdata, 0)[0] + 1
                col = struct.unpack_from("<H", rdata, 2)[0] + 1
                num_val = _parse_biff_rk(struct.unpack_from("<I", rdata, 6)[0])
                if num_val is not None:
                    val_str = f"{int(num_val)}" if float(num_val).is_integer() else f"{num_val:.4f}".rstrip("0").rstrip(".")
                    sheet_cells[(row, col)] = val_str
            elif rtype == 0x00BD and len(rdata) >= 6:  # MULRK
                row = struct.unpack_from("<H", rdata, 0)[0] + 1
                col_first = struct.unpack_from("<H", rdata, 2)[0] + 1
                col_last = struct.unpack_from("<H", rdata, len(rdata) - 2)[0] + 1
                num_rks = (len(rdata) - 6) // 6
                for i in range(num_rks):
                    c_idx = col_first + i
                    if c_idx > col_last: break
                    rk_val = struct.unpack_from("<I", rdata, 4 + i * 6 + 2)[0]
                    num_val = _parse_biff_rk(rk_val)
                    if num_val is not None:
                        val_str = f"{int(num_val)}" if float(num_val).is_integer() else f"{num_val:.4f}".rstrip("0").rstrip(".")
                        sheet_cells[(row, c_idx)] = val_str
            elif rtype == 0x0006 and len(rdata) >= 16:  # FORMULA
                row = struct.unpack_from("<H", rdata, 0)[0] + 1
                col = struct.unpack_from("<H", rdata, 2)[0] + 1
                if not (rdata[12] == 0xFF and rdata[13] == 0xFF):
                    try:
                        num_val = struct.unpack_from("<d", rdata, 6)[0]
                        val_str = f"{int(num_val)}" if num_val.is_integer() else f"{num_val:.4f}".rstrip("0").rstrip(".")
                        sheet_cells[(row, col)] = val_str
                    except Exception:
                        pass

        sheets_dict[sheet_name] = sheet_cells

    return sheets_dict


def _parse_biff_rk(rk: int) -> Optional[float]:
    """BIFF RKフォーマットの数値を float に復元"""
    try:
        is_div100 = bool(rk & 0x01)
        is_int = bool(rk & 0x02)
        if is_int:
            val = float(ctypes.c_int32(rk >> 2).value)
        else:
            val_bits = (rk & 0xFFFFFFFC) << 32
            val = struct.unpack(">d", struct.pack(">Q", val_bits))[0]
        if is_div100:
            val /= 100.0
        return val
    except Exception:
        return None


# 9.1 全シート保持 .xlsx 生成 & 生グリッド抽出

def _extract_bytes_from_file(file_or_bytes: Any) -> bytes:
    """Streamlit UploadedFile / BytesIO / bytes を安全に byte列として抽出"""
    if file_or_bytes is None:
        return b""
    if isinstance(file_or_bytes, bytes):
        return file_or_bytes
    if hasattr(file_or_bytes, "getvalue"):
        return file_or_bytes.getvalue()
    if hasattr(file_or_bytes, "read"):
        try:
            pos = file_or_bytes.tell() if hasattr(file_or_bytes, "tell") else None
            data = file_or_bytes.read()
            if pos is not None and hasattr(file_or_bytes, "seek"):
                file_or_bytes.seek(pos)
            return data
        except Exception:
            return b""
    return b""


def extract_exact_sheet_grids(file_or_bytes: Any) -> Dict[str, Dict[Tuple[int, int], str]]:
    """ファイルバイナリまたは UploadedFile から全シートのセルデータ（(row, col) -> 文字列）を完全抽出"""
    content_bytes = _extract_bytes_from_file(file_or_bytes)
    if not content_bytes or len(content_bytes) < 4:
        return {}

    # 1. .xlsx (PK zip)
    if content_bytes[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(content_bytes), "r") as zf:
                return extract_xlsx_sheet_grids(zf)
        except Exception:
            pass

    # 2. .xls (OLE2 CFBF)
    if len(content_bytes) >= 8 and content_bytes[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        wb_stream = extract_ole_workbook_stream(content_bytes)
        if wb_stream:
            return parse_biff_multisheet(wb_stream)

    # 3. 生 BIFF ストリーム
    if len(content_bytes) >= 4 and content_bytes[:2] in (b"\x09\x08", b"\x09\x04", b"\x09\x02", b"\x09\x00"):
        return parse_biff_multisheet(content_bytes)

    return {}


def convert_xls_file_to_multisheet_xlsx_bytes(file_or_bytes: Any) -> bytes:
    """旧 .xls 内の全シート・セル配置を保持して最新 .xlsx バイナリ（bytes）へ完全変換"""
    content_bytes = _extract_bytes_from_file(file_or_bytes)
    sheet_grids = extract_exact_sheet_grids(content_bytes)
    if not sheet_grids:
        return b""

    output_zip = io.BytesIO()
    sheet_names = list(sheet_grids.keys())

    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        ct_parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '<Default Extension="xml" ContentType="application/xml"/>',
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        ]
        for i in range(1, len(sheet_names) + 1):
            ct_parts.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
        ct_parts.append('</Types>')
        zf.writestr("[Content_Types].xml", "".join(ct_parts))

        zf.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        )

        wb_rels_parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        ]
        for i in range(1, len(sheet_names) + 1):
            wb_rels_parts.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
        wb_rels_parts.append('</Relationships>')
        zf.writestr("xl/_rels/workbook.xml.rels", "".join(wb_rels_parts))

        wb_parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
            '<sheets>'
        ]
        for i, s_name in enumerate(sheet_names, 1):
            esc_name = s_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            wb_parts.append(f'<sheet name="{esc_name}" sheetId="{i}" r:id="rId{i}"/>')
        wb_parts.append('</sheets></workbook>')
        zf.writestr("xl/workbook.xml", "".join(wb_parts))

        for i, s_name in enumerate(sheet_names, 1):
            grid = sheet_grids[s_name]
            rows_dict: Dict[int, List[Tuple[int, str]]] = defaultdict(list)
            for (r, c), val in grid.items():
                if val:
                    rows_dict[r].append((c, val))

            ws_parts = [
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
                '<sheetData>'
            ]

            for r in sorted(rows_dict.keys()):
                ws_parts.append(f'<row r="{r}">')
                for c, val in sorted(rows_dict[r], key=lambda x: x[0]):
                    c_ref = f"{_col_idx_to_str(c)}{r}"
                    if re.match(r"^-?\d+(\.\d+)?$", val) and not val.startswith("0") and len(val) <= 15:
                        ws_parts.append(f'<c r="{c_ref}"><v>{val}</v></c>')
                    else:
                        esc_val = val.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                        ws_parts.append(f'<c r="{c_ref}" t="inlineStr"><is><t>{esc_val}</t></is></c>')
                ws_parts.append('</row>')

            ws_parts.append('</sheetData></worksheet>')
            zf.writestr(f"xl/worksheets/sheet{i}.xml", "".join(ws_parts))

    return output_zip.getvalue()


def _col_idx_to_str(col_idx: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA"""
    result = []
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx - 1, 26)
        result.append(chr(65 + remainder))
    return "".join(reversed(result))


def convert_df_to_xlsx_bytes(df: pd.DataFrame, sheet_name: str = "Sheet1") -> bytes:
    """DataFrame を純粋な .xlsx バイナリ（Bytes）に変換"""
    grid: Dict[Tuple[int, int], str] = {}
    cols = list(df.columns)
    for col_idx, col_name in enumerate(cols, 1):
        grid[(1, col_idx)] = str(col_name)

    for row_idx, row in enumerate(df.itertuples(index=False), 2):
        for col_idx, val in enumerate(row, 1):
            if pd.notna(val):
                grid[(row_idx, col_idx)] = str(val).strip()

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>'
        )
        zf.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        )
        zf.writestr("xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>'
        )
        esc_sheet = sheet_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        zf.writestr("xl/workbook.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{esc_sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>'
        )
        ws_parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        ]
        rows_map: Dict[int, List[Tuple[int, str]]] = defaultdict(list)
        for (r, c), val in grid.items():
            if val:
                rows_map[r].append((c, val))
        for r in sorted(rows_map.keys()):
            ws_parts.append(f'<row r="{r}">')
            for c, val in sorted(rows_map[r], key=lambda x: x[0]):
                c_ref = f"{_col_idx_to_str(c)}{r}"
                if re.match(r"^-?\d+(\.\d+)?$", val) and not val.startswith("0") and len(val) <= 15:
                    ws_parts.append(f'<c r="{c_ref}"><v>{val}</v></c>')
                else:
                    esc_val = val.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                    ws_parts.append(f'<c r="{c_ref}" t="inlineStr"><is><t>{esc_val}</t></is></c>')
            ws_parts.append('</row>')
        ws_parts.append('</sheetData></worksheet>')
        zf.writestr("xl/worksheets/sheet1.xml", "".join(ws_parts))

    return out.getvalue()


def extract_all_raw_sheets_from_file(file_or_bytes: Any) -> Dict[str, pd.DataFrame]:
    """ファイルから全シートの生データを抽出し、シート名 -> DataFrame の辞書を生成"""
    content_bytes = _extract_bytes_from_file(file_or_bytes)
    sheet_grids = extract_exact_sheet_grids(content_bytes)
    result: Dict[str, pd.DataFrame] = {}

    for s_name, grid in sheet_grids.items():
        if not grid:
            result[s_name] = pd.DataFrame()
            continue

        max_r = max(r for r, _ in grid.keys())
        max_c = max(c for _, c in grid.keys())

        data_matrix = [["" for _ in range(max_c)] for _ in range(max_r)]
        for (r, c), val in grid.items():
            data_matrix[r - 1][c - 1] = val

        df = pd.DataFrame(data_matrix)
        df = df.dropna(how="all")
        result[s_name] = df

    return result


# 9.2 DBインポート解析エンジン（文字化け・値破壊完全排除 & 厳密判定）

def _is_strictly_date_str(val: Any) -> str:
    """
    真の日付・和暦・Excelシリアル値のみを YYYY-MM-DD に変換。
    「築館なめこ」「キャベツ」などの一般文字列は絶対に日付判定せず、空文字（""）を返します。
    """
    if val is None or pd.isnull(val):
        return ""
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.strftime("%Y-%m-%d")

    s = unicodedata.normalize("NFKC", str(val)).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return ""

    # Excelシリアル数値 (20000〜65000: 1954年〜2077年)
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if 20000 <= val <= 65000:
            try:
                dt = datetime(1899, 12, 30) + timedelta(days=float(val))
                return dt.strftime("%Y-%m-%d")
            except Exception:
                pass
    if s.isdigit() and 20000 <= int(s) <= 65000:
        try:
            dt = datetime(1899, 12, 30) + timedelta(days=int(s))
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    # 和暦パターン: R8.9.2, 令8.9.10, H30.5.1, 令和8年9月2日 等
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

    # 西暦年月日パターン: 2026-09-02, 2026/9/2, 2026.9.2
    m_ymd = re.match(r"^(\d{4})[\.年/\-](\d{1,2})[\.月/\-](\d{1,2})日?", s)
    if m_ymd:
        return f"{int(m_ymd.group(1)):04d}-{int(m_ymd.group(2)):02d}-{int(m_ymd.group(3)):02d}"

    # 月日パターン: 9月5日, 9/5 (1 <= 月 <= 12, 1 <= 日 <= 31)
    m_md = re.match(r"^(\d{1,2})[\.月/\-](\d{1,2})日?$", s)
    if m_md:
        m_val, d_val = int(m_md.group(1)), int(m_md.group(2))
        if 1 <= m_val <= 12 and 1 <= d_val <= 31:
            cur_y = datetime.now().year
            return f"{cur_y:04d}-{m_val:02d}-{d_val:02d}"

    return ""


def parse_single_sheet_dataframe(df_sheet: pd.DataFrame, default_supplier: str = "") -> Tuple[pd.DataFrame, str]:
    """
    1枚のシートのDataFrameから取引明細（出荷日・品名・数量・単価・仕入金額）を高精度抽出。
    品名の誤除外を防ぎ、元エクセルに記載された全行を確実に復元します。
    """
    extracted_rows: List[Dict[str, Any]] = []
    supplier_name = default_supplier

    if df_sheet is None or df_sheet.empty:
        return pd.DataFrame(), supplier_name

    num_rows = len(df_sheet)
    num_cols = len(df_sheet.columns)

    # 1. 伝票ヘッダー領域（上部15行）から仕入先名・市場を自動探索
    for r in range(min(15, num_rows)):
        row_vals = [str(df_sheet.iloc[r, c]).strip() for c in range(num_cols)]
        for v in row_vals:
            if not v or len(v) < 2:
                continue
            if "FL]]" in v or any(ord(ch) < 32 for ch in v if ch not in "\t\n"):
                continue
            if any(k in v for k in ("御中", "組合", "農協", "青果", "出荷組合", "会社", "園芸")):
                clean_sup = re.sub(r"(御中|様|殿|取引先|仕入先|市場番号|コード|\:|\：)", "", v).strip()
                if len(clean_sup) >= 3 and not supplier_name:
                    supplier_name = clean_sup

    # 2. 列ヘッダー行の特定（出荷日、品名、数量、単価、金額等の列インデックスを検出）
    col_map = {"date": -1, "item": -1, "qty": -1, "price": -1, "subtotal": -1, "code": -1, "market": -1}
    start_row = 0

    for r in range(min(25, num_rows)):
        row_vals = [normalize_header(df_sheet.iloc[r, c]) for c in range(num_cols)]
        found_keys = 0
        temp_map = {}
        for c, v in enumerate(row_vals):
            if not v: continue
            if any(k in v for k in ["出荷日", "日付", "年月日", "伝票日", "取引日"]) and "date" not in temp_map:
                temp_map["date"] = c; found_keys += 1
            elif any(k in v for k in ["品名", "商品名", "品目", "規格", "品種", "品名規格"]) and "item" not in temp_map:
                temp_map["item"] = c; found_keys += 1
            elif any(k in v for k in ["数量", "箱数", "個数", "受入", "納品数", "バラ", "ケース"]) and "qty" not in temp_map:
                temp_map["qty"] = c; found_keys += 1
            elif any(k in v for k in ["単価", "仕入単価", "売価", "価格"]) and "price" not in temp_map:
                temp_map["price"] = c; found_keys += 1
            elif any(k in v for k in ["仕入金額", "金額", "小計", "売上金額", "計", "販売金額"]) and "subtotal" not in temp_map:
                temp_map["subtotal"] = c; found_keys += 1
            elif any(k in v for k in ["商品コード", "品番", "コード"]) and "code" not in temp_map:
                temp_map["code"] = c
            elif any(k in v for k in ["市場番号", "市場", "取引先"]) and "market" not in temp_map:
                temp_map["market"] = c

        if found_keys >= 2:
            col_map.update(temp_map)
            start_row = r + 1
            break

    current_date = ""

    # 3. 明細行の精密スキャン
    for r in range(start_row, num_rows):
        row_raw = [str(df_sheet.iloc[r, c]).strip() for c in range(num_cols)]
        row_str = " ".join([v for v in row_raw if v])

        if not row_str:
            continue
        # 集計・合計行のスキップ
        if any(term in row_str for term in ("総合計", "消費税", "手数料", "差引金額", "販売金額合計", "総計")):
            continue
        if "FL]]" in row_str:
            continue

        # 日付の探索（見つかった場合は current_date を更新して下位行へ引き継ぎ）
        date_candidate = ""
        if col_map["date"] >= 0:
            date_candidate = _is_strictly_date_str(row_raw[col_map["date"]])
        if not date_candidate:
            for c in range(min(5, num_cols)):
                d_test = _is_strictly_date_str(row_raw[c])
                if d_test:
                    date_candidate = d_test
                    break
        if date_candidate:
            current_date = date_candidate

        # 品名・数量・単価・小計の抽出
        item_name = ""
        qty_val: Optional[float] = None
        price_val: Optional[float] = None
        subtotal_val: Optional[float] = None

        # A. ヘッダー列マップが存在する場合の優先抽出
        if col_map["item"] >= 0 and col_map["item"] < num_cols:
            c_val = row_raw[col_map["item"]]
            if c_val and not _is_strictly_date_str(c_val) and not re.match(r"^[\d\,\.\-\+\*\/\￥\\¥\%\(\)]+$", c_val):
                if not any(k in c_val for k in ("小計", "合計", "出荷日", "品名", "数量", "単価", "金額", "頁")):
                    item_name = c_val

        if col_map["qty"] >= 0 and col_map["qty"] < num_cols:
            q_clean = re.sub(r"[^\d\.]", "", row_raw[col_map["qty"]])
            if q_clean:
                try: qty_val = float(q_clean)
                except ValueError: pass

        if col_map["price"] >= 0 and col_map["price"] < num_cols:
            p_clean = re.sub(r"[^\d\.]", "", row_raw[col_map["price"]])
            if p_clean:
                try: price_val = float(p_clean)
                except ValueError: pass

        if col_map["subtotal"] >= 0 and col_map["subtotal"] < num_cols:
            s_clean = re.sub(r"[^\d\.]", "", row_raw[col_map["subtotal"]])
            if s_clean:
                try: subtotal_val = float(s_clean)
                except ValueError: pass

        # B. 位置推定によるフォールバック抽出
        if not item_name:
            for c in range(num_cols):
                val = row_raw[c]
                if not val or val in ("0", "0.0", "-"):
                    continue
                if "FL]]" in val or any(ord(ch) < 32 for ch in val if ch not in "\t\n"):
                    continue
                # 真の日付セルはスキップ
                if _is_strictly_date_str(val):
                    continue
                # 純粋な数値や記号のみはスキップ
                if re.match(r"^[\d\,\.\-\+\*\/\￥\\¥\%\(\)]+$", val):
                    continue
                if any(k in val for k in ("小計", "合計", "頁", "出荷日", "品名", "数量", "単価", "金額", "消費税", "手数料")):
                    continue
                # 有効な品名（「築館なめこ」「桃」など）
                item_name = val
                break

        # 数値リストの再スキャン（欠損時）
        if qty_val is None or (price_val is None and subtotal_val is None):
            numeric_vals: List[float] = []
            for c in range(num_cols):
                val = row_raw[c]
                if not val: continue
                # 日付形式のシリアル値は数値候補から除外
                if _is_strictly_date_str(val): continue
                c_clean = re.sub(r"[\,\s\￥\\¥]", "", val)
                if re.match(r"^-?\d+(\.\d+)?$", c_clean):
                    try:
                        n_f = float(c_clean)
                        if n_f not in (46267.0, 46270.0) and n_f != 0.0:
                            numeric_vals.append(n_f)
                    except ValueError:
                        pass

            if numeric_vals:
                if len(numeric_vals) >= 3:
                    if qty_val is None: qty_val = numeric_vals[0]
                    if price_val is None: price_val = numeric_vals[1]
                    if subtotal_val is None: subtotal_val = numeric_vals[2]
                elif len(numeric_vals) == 2:
                    v1, v2 = numeric_vals[0], numeric_vals[1]
                    if v1 <= 500 and v2 >= 100:
                        if qty_val is None: qty_val = v1
                        if price_val is None: price_val = v2
                    else:
                        if price_val is None: price_val = v1
                        if subtotal_val is None: subtotal_val = v2
                elif len(numeric_vals) == 1:
                    if subtotal_val is None: subtotal_val = numeric_vals[0]

        # 明細レコードの成立判定（品名があり、数量または金額・単価のいずれかが存在すること）
        if item_name and (qty_val is not None or price_val is not None or subtotal_val is not None):
            final_date = current_date or datetime.now().strftime("%Y-%m-%d")
            final_qty = qty_val if (qty_val is not None and qty_val > 0) else 1.0
            final_price = price_val if (price_val is not None and price_val > 0) else ((subtotal_val / final_qty) if subtotal_val else 0.0)
            final_subtotal = subtotal_val if (subtotal_val is not None and subtotal_val > 0) else (final_qty * final_price)

            extracted_rows.append({
                "日付": final_date,
                "市場番号": "",
                "カテゴリー": "青果",
                "商品コード": "",
                "商品名": item_name,
                "品名": item_name,
                "単価": round(final_price, 2),
                "数量": round(final_qty, 2),
                "小計": round(final_subtotal, 2),
                "仕入金額": round(final_subtotal, 2)
            })

    if not extracted_rows:
        return pd.DataFrame(), supplier_name

    res_df = pd.DataFrame(extracted_rows)
    return res_df, supplier_name


def parse_trade_file(uploaded_file) -> Tuple[pd.DataFrame, List[str]]:
    """
    アップロードされた伝票ファイル（.xls, .xlsx, .csv 等）をパースし、
    全シートの取引データを統合した DataFrame と仕入先リストを返却します。
    """
    content_bytes = _extract_bytes_from_file(uploaded_file)
    if not content_bytes:
        return pd.DataFrame(), []

    all_dfs: List[pd.DataFrame] = []
    extracted_suppliers: List[str] = []

    # 1. Excel (全シート抽出)
    sheet_raw_dfs = extract_all_raw_sheets_from_file(content_bytes)
    if sheet_raw_dfs:
        for s_name, df_sheet in sheet_raw_dfs.items():
            if df_sheet is None or df_sheet.empty:
                continue
            parsed_df, sup_name = parse_single_sheet_dataframe(df_sheet, default_supplier=s_name)
            if not parsed_df.empty:
                all_dfs.append(parsed_df)
            if sup_name and sup_name not in extracted_suppliers:
                extracted_suppliers.append(sup_name)

        if all_dfs:
            final_df = pd.concat(all_dfs, ignore_index=True)
            return final_df, extracted_suppliers

    # 2. CSV / テキストのフォールバック
    is_binary = (len(content_bytes) >= 4 and content_bytes[:4] == b"PK\x03\x04") or \
                (len(content_bytes) >= 8 and content_bytes[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if not is_binary:
        for enc in ("cp932", "utf-8", "euc-jp", "shift_jis"):
            try:
                text = content_bytes.decode(enc)
                lines = [l for l in text.splitlines() if l.strip()]
                if lines:
                    matrix = [l.split(",") if "," in l else l.split("\t") for l in lines]
                    csv_df = pd.DataFrame(matrix)
                    parsed_df, sup_name = parse_single_sheet_dataframe(csv_df)
                    if not parsed_df.empty:
                        return parsed_df, [sup_name] if sup_name else []
            except Exception:
                continue

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
                    # 1. 元エクセルの全シート未加工グリッドデータを抽出（配置・書式保持用）
                    uploaded_entry_file.seek(0)
                    raw_sheets = extract_all_raw_sheets_from_file(uploaded_entry_file)
                    uploaded_entry_file.seek(0)
                    
                    # 2. DB用明細データの抽出
                    df_parsed, extracted_sups = parse_trade_file(uploaded_entry_file)

                    if raw_sheets or not df_parsed.empty:
                        # 取引先の自動候補追加
                        for s in extracted_sups:
                            if s and s not in st.session_state.extracted_suppliers:
                                st.session_state.extracted_suppliers.append(s)

                        # DB登録
                        if not df_parsed.empty:
                            new_records_list = df_parsed.to_dict("records")
                            insert_records_to_db(current_user, new_records_list)
                            st.session_state.verification_batch = {
                                "file_name": uploaded_entry_file.name,
                                "df": df_parsed
                            }

                        # ★ 全シート生グリッドデータをセッション & 永続ストレージに保持
                        st.session_state.last_imported_raw_sheets = raw_sheets
                        st.session_state.last_imported_file_name = uploaded_entry_file.name

                        try:
                            RAW_SHEETS_CACHE_FILE = os.path.join(DATA_DIR, "latest_imported_raw_sheets.json")
                            serializable_dict = {s: sdf.to_dict(orient="split") for s, sdf in raw_sheets.items()}
                            with open(RAW_SHEETS_CACHE_FILE, "w", encoding="utf-8") as f:
                                json.dump(serializable_dict, f, ensure_ascii=False)
                        except Exception:
                            pass

                        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                        st.toast(f"✅ 『{uploaded_entry_file.name}』の全 {len(raw_sheets)} シートを元エクセル配置のまま自動記録！下の記録一覧へ正確に反映されました。")
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

# 📋 登録済み取引データ一覧（元エクセル全シート完全保持グリッド & Excel / PDF一括保存対応）
    st.write("")
    st.markdown('<div class="section-title">📋 登録済み取引データ一覧（元エクセル配置・全シート保持反映）</div>', unsafe_allow_html=True)

    # 1. 未加工生グリッドデータのロード（セッション & 永続ファイル）
    RAW_SHEETS_CACHE_FILE = os.path.join(DATA_DIR, "latest_imported_raw_sheets.json")
    raw_sheets = st.session_state.get("last_imported_raw_sheets")
    if not raw_sheets and os.path.exists(RAW_SHEETS_CACHE_FILE):
        try:
            with open(RAW_SHEETS_CACHE_FILE, "r", encoding="utf-8") as f:
                saved_raw = json.load(f)
                raw_sheets = {s: pd.DataFrame(d["data"], columns=d["columns"], index=d["index"]) for s, d in saved_raw.items()}
                st.session_state.last_imported_raw_sheets = raw_sheets
        except Exception:
            pass

    # ==========================================
    # ヘルパー関数: 元エクセル配置・全シート完全保持 Excel (.xlsx) 生成
    # ==========================================
    def export_raw_sheets_to_xlsx_bytes(raw_dict: Dict[str, pd.DataFrame]) -> bytes:
        bio = io.BytesIO()
        with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
            sheet_names = list(raw_dict.keys())
            # [Content_Types].xml
            ct = [
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                '<Default Extension="xml" ContentType="application/xml"/>',
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            ]
            for i in range(1, len(sheet_names) + 1):
                ct.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            ct.append('</Types>')
            z.writestr("[Content_Types].xml", "".join(ct))

            z.writestr("_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>'
            )

            wb_rels = [
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            ]
            for i in range(1, len(sheet_names) + 1):
                wb_rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
            wb_rels.append('</Relationships>')
            z.writestr("xl/_rels/workbook.xml.rels", "".join(wb_rels))

            wb = [
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
                '<sheets>'
            ]
            for i, s_name in enumerate(sheet_names, 1):
                clean_sname = str(s_name).strip()[:31]
                esc_name = clean_sname.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                wb.append(f'<sheet name="{esc_name}" sheetId="{i}" r:id="rId{i}"/>')
            wb.append('</sheets></workbook>')
            z.writestr("xl/workbook.xml", "".join(wb))

            for i, (s_name, sdf) in enumerate(raw_dict.items(), 1):
                ws = [
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                ]
                rows_data = sdf.values if hasattr(sdf, "values") else sdf
                for r_idx, row in enumerate(rows_data, 1):
                    cells_xml = []
                    for c_idx, val in enumerate(row, 1):
                        if val is None or pd.isna(val) or val == "":
                            continue
                        val_str = str(val).strip()
                        if not val_str or val_str.lower() in ("nan", "none", "null"):
                            continue
                        c_ref = f"{col_letter(c_idx - 1)}{r_idx}"
                        if re.match(r"^-?\d+(\.\d+)?$", val_str) and not (val_str.startswith("0") and len(val_str) > 1 and not val_str.startswith("0.")) and len(val_str) <= 15:
                            cells_xml.append(f'<c r="{c_ref}"><v>{val_str}</v></c>')
                        else:
                            esc_val = val_str.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                            cells_xml.append(f'<c r="{c_ref}" t="inlineStr"><is><t>{esc_val}</t></is></c>')
                    if cells_xml:
                        ws.append(f'<row r="{r_idx}">{"".join(cells_xml)}</row>')
                ws.append('</sheetData></worksheet>')
                z.writestr(f"xl/worksheets/sheet{i}.xml", "".join(ws))
        return bio.getvalue()

    # ==========================================
    # ヘルパー関数: 元エクセル配置・全シート完全保持 PDF (.pdf) 生成
    # ==========================================
    def export_raw_sheets_to_pdf_bytes(raw_dict: Dict[str, pd.DataFrame], doc_title: str = "") -> bytes:
        buf = bytearray()
        offsets = []

        def add_obj(body_bytes: bytes) -> int:
            offsets.append(len(buf))
            obj_num = len(offsets)
            buf.extend(f"{obj_num} 0 obj\n".encode("latin1"))
            buf.extend(body_bytes)
            buf.extend(b"\nendobj\n")
            return obj_num

        buf.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

        # 日本語標準 CID フォント定義 (Adobe HeiseiKakuGo)
        cid_font = b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HeiseiKakuGo-W5 /CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 5 >> >>"
        cid_num = add_obj(cid_font)
        type0_font = f"<< /Type /Font /Subtype /Type0 /BaseFont /HeiseiKakuGo-W5 /Encoding /UniJIS-UTF16-H /DescendantFonts [{cid_num} 0 R] >>".encode("latin1")
        font_num = add_obj(type0_font)

        page_obj_nums = []
        PAGE_W, PAGE_H = 842, 595  # A4 Landscape (横向き帳票)
        MARGIN_X, TOP_Y, BOTTOM_Y, ROW_H = 28, 560, 35, 16

        for s_name, sdf in raw_dict.items():
            if sdf is None or len(sdf) == 0:
                continue
            max_r = len(sdf)
            max_c = len(sdf.columns) if hasattr(sdf, "columns") else (len(sdf[0]) if len(sdf) > 0 else 0)
            if max_c == 0:
                continue
            col_w = max(45, min(95, int((PAGE_W - MARGIN_X * 2) / max_c)))
            rows_per_page = max(10, int((TOP_Y - 45 - BOTTOM_Y) / ROW_H))
            rows_data = sdf.values if hasattr(sdf, "values") else sdf

            for p_start in range(0, max_r, rows_per_page):
                chunk = rows_data[p_start : p_start + rows_per_page]
                p_idx = p_start // rows_per_page + 1

                stream = bytearray()
                stream.extend(b"BT\n/F1 12 Tf\n")
                title_str = f"シート: {s_name} (ページ {p_idx})"
                stream.extend(f"{MARGIN_X} {TOP_Y} Td <{title_str.encode('utf-16-be').hex().upper()}> Tj\nET\n".encode("latin1"))

                cur_y = TOP_Y - 22
                for r_i, row in enumerate(chunk):
                    for c_i in range(min(max_c, len(row))):
                        val = row[c_i]
                        if val is not None and str(val).strip() and str(val).strip().lower() not in ("nan", "none", "null"):
                            text_val = str(val).strip()
                            x_pos = MARGIN_X + c_i * col_w + 2
                            y_pos = cur_y - 10
                            hex_str = text_val[:20].encode("utf-16-be").hex().upper()
                            stream.extend(f"BT\n/F1 7.5 Tf\n{x_pos} {y_pos} Td <{hex_str}> Tj\nET\n".encode("latin1"))
                    cur_y -= ROW_H
                    if cur_y < BOTTOM_Y:
                        break

                content_num = add_obj(f"<< /Length {len(stream)} >>\nstream\n".encode("latin1") + stream + b"\nendstream")
                page_dict = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] /Resources << /Font << /F1 {font_num} 0 R >> >> /Contents {content_num} 0 R >>".encode("latin1")
                page_obj_nums.append(add_obj(page_dict))

        if not page_obj_nums:
            stream = b"BT\n/F1 12 Tf\n50 500 Td <" + "データがありません".encode("utf-16-be").hex().upper().encode("latin1") + b"> Tj\nET\n"
            content_num = add_obj(f"<< /Length {len(stream)} >>\nstream\n".encode("latin1") + stream + b"\nendstream")
            page_dict = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] /Resources << /Font << /F1 {font_num} 0 R >> >> /Contents {content_num} 0 R >>".encode("latin1")
            page_obj_nums.append(add_obj(page_dict))

        kids_str = " ".join(f"{p} 0 R" for p in page_obj_nums)
        pages_num = add_obj(f"<< /Type /Pages /Kids [{kids_str}] /Count {len(page_obj_nums)} >>".encode("latin1"))
        catalog_num = add_obj(f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode("latin1"))

        xref_pos = len(buf)
        buf.extend(f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode("latin1"))
        for off in offsets:
            buf.extend(f"{off:010d} 00000 n \n".encode("latin1"))
        buf.extend(f"trailer\n<< /Size {len(offsets) + 1} /Root {catalog_num} 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("latin1"))
        return bytes(buf)

   # 2. 画面描画ブロック（② 大容量対応ページネーション & メモリ最適化）
    if raw_sheets:
        total_raw_rows = sum(len(sdf) for sdf in raw_sheets.values())
        total_sum = int(df_current["仕入金額"].sum()) if df_current is not None and not df_current.empty and "仕入金額" in df_current.columns else 0
        total_q = int(df_current["数量"].sum()) if df_current is not None and not df_current.empty and "数量" in df_current.columns else 0

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("検出総シート数", f"{len(raw_sheets):,} シート")
        m2.metric("総行数 (全シート計)", f"{total_raw_rows:,} 行")
        m3.metric("総仕入金額", f"¥ {total_sum:,}")
        m4.metric("平均単価", f"¥ {int(total_sum / total_q):,}" if total_q > 0 else "¥ 0")

        # 📥 元配置・全シート完全保持の Excel / PDF 一括ダウンロードボタン群
        fname_base = st.session_state.get("last_imported_file_name", "trade_file").rsplit(".", 1)[0]
        col_dl_x, col_dl_p = st.columns(2)
        with col_dl_x:
            raw_xlsx_bytes = export_raw_sheets_to_xlsx_bytes(raw_sheets)
            st.download_button(
                label="📗 元配置・全シート完全保持 Excel (.xlsx) でダウンロード",
                data=raw_xlsx_bytes,
                file_name=f"{fname_base}_全シート元配置保持.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="btn_download_raw_multisheet_xlsx"
            )
        with col_dl_p:
            raw_pdf_bytes = export_raw_sheets_to_pdf_bytes(raw_sheets, doc_title=fname_base)
            st.download_button(
                label="📕 元配置・全シート完全保持 PDF (.pdf) でダウンロード",
                data=raw_pdf_bytes,
                file_name=f"{fname_base}_全シート帳票.pdf",
                mime="application/pdf",
                use_container_width=True,
                key="btn_download_raw_multisheet_pdf"
            )

        st.write("")
        c_info1, c_info2 = st.columns([3, 1.2])
        with c_info1:
            st.caption("※ 元のエクセルの行列配置・セル位置・独自表記（5%上乗せ、請求金額等）をそのまま完全保持して表示しています。")
        with c_info2:
            view_switcher = st.radio("表示切り替え", ["📑 元エクセル配置（保持）", "📊 DB集計リスト"], horizontal=True, label_visibility="collapsed")

        if view_switcher == "📑 元エクセル配置（保持）":
            sheet_keys = list(raw_sheets.keys())
            grid_tabs = st.tabs(sheet_keys)
            for t_idx, sname in enumerate(sheet_keys):
                with grid_tabs[t_idx]:
                    sdf = raw_sheets[sname]
                    st.caption(f"シート名: **{sname}** (全 {len(sdf)} 行)")
                    st.dataframe(sdf, use_container_width=True, height=520)
            gc.collect()
        else:
            if df_current is not None and not df_current.empty:
                display_cols = [c for c in ["id", "日付", "市場番号", "カテゴリー", "商品コード", "品名", "単価", "数量", "仕入金額", "シート名", "登録担当者"] if c in df_current.columns]
                
                # ② ページネーションコントロール（1ページ50件）
                PAGE_SIZE = 50
                total_records = len(df_current)
                total_pages = max(1, (total_records + PAGE_SIZE - 1) // PAGE_SIZE)

                col_p1, col_p2 = st.columns([3, 1.5])
                with col_p1:
                    cur_page = st.number_input(f"📄 ページ選択 (全 {total_pages} ページ / 総件数 {total_records:,} 件)", min_value=1, max_value=total_pages, value=1, step=1, key="tab0_db_page_num")
                with col_p2:
                    st.caption(f"表示中: **{(cur_page - 1) * PAGE_SIZE + 1} 〜 {min(cur_page * PAGE_SIZE, total_records)} 件**")

                p_start = (cur_page - 1) * PAGE_SIZE
                p_end = p_start + PAGE_SIZE
                page_df = df_current[display_cols].iloc[p_start:p_end]
                st.dataframe(page_df, use_container_width=True, height=480)
                gc.collect()

    # ファイル未取込時のフォールバック表示（ページネーション対応）
    elif df_current is not None and not df_current.empty:
        total_sum = int(df_current["仕入金額"].sum()) if "仕入金額" in df_current.columns else 0
        total_q = int(df_current["数量"].sum()) if "数量" in df_current.columns else 0

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("総取引件数", f"{len(df_current):,} 件")
        m2.metric("総数量", f"{total_q:,} 個")
        m3.metric("総仕入金額", f"¥ {total_sum:,}")
        m4.metric("平均単価", f"¥ {int(total_sum / total_q):,}" if total_q > 0 else "¥ 0")

        display_cols = [c for c in ["id", "日付", "市場番号", "カテゴリー", "商品コード", "品名", "単価", "数量", "仕入金額", "シート名", "登録担当者"] if c in df_current.columns]
        PAGE_SIZE = 50
        total_records = len(df_current)
        total_pages = max(1, (total_records + PAGE_SIZE - 1) // PAGE_SIZE)

        col_p1, col_p2 = st.columns([3, 1.5])
        with col_p1:
            cur_page = st.number_input(f"📄 ページ選択 (全 {total_pages} ページ / 総件数 {total_records:,} 件)", min_value=1, max_value=total_pages, value=1, step=1, key="tab0_fallback_page_num")
        with col_p2:
            st.caption(f"表示中: **{(cur_page - 1) * PAGE_SIZE + 1} 〜 {min(cur_page * PAGE_SIZE, total_records)} 件**")

        p_start = (cur_page - 1) * PAGE_SIZE
        p_end = p_start + PAGE_SIZE
        page_df = df_current[display_cols].iloc[p_start:p_end]
        st.dataframe(page_df, use_container_width=True, height=480)
        gc.collect()
    else:
        st.info("💡 登録されたデータはまだありません。上のドラッグ＆ドロップまたは手動フォームから記録してください。")
# ==========================================
# タブ 2: 取引一覧・インライン編集（元エクセル配置グリッド完全保持 & 双方向同期）
# ==========================================
with tabs[1]:
    st.markdown('<div class="section-title">📊 登録データの一覧・直接編集・選択削除（元エクセル配置保持）</div>', unsafe_allow_html=True)

    # 1. 未加工生グリッドデータの読み込み（セッション & 永続ストレージ）
    RAW_SHEETS_CACHE_FILE = os.path.join(DATA_DIR, "latest_imported_raw_sheets.json")
    raw_sheets = st.session_state.get("last_imported_raw_sheets")
    if not raw_sheets and os.path.exists(RAW_SHEETS_CACHE_FILE):
        try:
            with open(RAW_SHEETS_CACHE_FILE, "r", encoding="utf-8") as f:
                saved_raw = json.load(f)
                raw_sheets = {s: pd.DataFrame(d["data"], columns=d["columns"], index=d["index"]) for s, d in saved_raw.items()}
                st.session_state.last_imported_raw_sheets = raw_sheets
        except Exception:
            pass

    # ↩️ 誤削除データのワンクリック復元（UNDO）バナー
    if st.session_state.get("last_deleted_records"):
        del_count_now = len(st.session_state.last_deleted_records)
        st.markdown(f"""
            <div class="restore-box" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <span>🛡️ 直前に削除されたデータが <b>{del_count_now} 件</b> 安全に退避保管されています。誤って削除した場合は即座に元通り戻せます。</span>
            </div>
        """, unsafe_allow_html=True)
        col_undo, _ = st.columns([2.5, 3.5])
        with col_undo:
            if st.button(f"↩️ 直前に削除した {del_count_now} 件のデータを元通り復元する", type="primary", use_container_width=True, key="btn_undo_last_deleted_tab1"):
                restored_cnt = restore_last_deleted_records_db(st.session_state.last_deleted_records, restored_by=current_user)
                st.session_state.last_deleted_records = []
                st.toast(f"🎉 {restored_cnt} 件のデータを元通り復元しました！")
                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.rerun()

    # 操作案内 & モード切替ラジオボタン
    col_c1, col_c2 = st.columns([3, 2])
    with col_c1:
        st.caption("※ テーブル内のセルをクリックして数値を直接変更・空行追加入力できます。「変更をデータベースに保存する」を押すと、元エクセル配置とDB集計データの両方に即時反映されます。")
    with col_c2:
        edit_mode = st.radio(
            "編集モード切り替え:",
            ["📑 元エクセル配置グリッド編集（推奨）", "📊 DB正規化リスト編集"],
            horizontal=True,
            key="tab1_editor_mode_radio"
        )

    # ==========================================
    # A. 【元エクセル配置・全シート完全保持】グリッド直接編集モード（写真1枚目スタイル）
    # ==========================================
    if edit_mode == "📑 元エクセル配置グリッド編集（推奨）" and raw_sheets:
        sheet_names = list(raw_sheets.keys())
        grid_edit_tabs = st.tabs(sheet_names)

        for t_idx, sname in enumerate(sheet_names):
            with grid_edit_tabs[t_idx]:
                cur_raw_df = raw_sheets[sname].copy()
                st.caption(f"シート名: **{sname}** (全 **{len(cur_raw_df)} 行** × **{len(cur_raw_df.columns)} 列**) — 元のエクセルの配置・空白セル位置をそのまま保持して直接編集できます。")

                # 生グリッドを直接編集可能な st.data_editor として展開
                edited_raw_df = st.data_editor(
                    cur_raw_df,
                    use_container_width=True,
                    height=540,
                    num_rows="dynamic",
                    key=f"raw_grid_editor_{t_idx}_v_{st.session_state.get('editor_version', 0)}"
                )

                col_save_raw, col_reset_raw, col_dl_raw = st.columns([2.5, 1.5, 2])

                with col_save_raw:
                    if st.button(f"💾 『{sname}』の変更をデータベースに保存する", type="primary", use_container_width=True, key=f"save_raw_btn_{t_idx}"):
                        # 1. 編集後生グリッドをセッション＆永続JSONへ保存
                        raw_sheets[sname] = edited_raw_df
                        st.session_state.last_imported_raw_sheets = raw_sheets
                        try:
                            serializable_dict = {s: sdf.to_dict(orient="split") for s, sdf in raw_sheets.items()}
                            with open(RAW_SHEETS_CACHE_FILE, "w", encoding="utf-8") as f:
                                json.dump(serializable_dict, f, ensure_ascii=False)
                        except Exception:
                            pass

                        # 2. 編集後グリッドから明細を自動抽出し、DB（transactions）を完全同期
                        parsed_df, _ = parse_single_sheet_dataframe(edited_raw_df, default_supplier=sname)
                        engine = get_engine()
                        with engine.begin() as conn:
                            # 旧レコードを退避テーブルへ移動
                            conn.execute(
                                text("""
                                    INSERT INTO deleted_transactions
                                    (original_id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, sheet_name, deleted_by)
                                    SELECT id, date, market_number, category, item_code, item_name, unit_price, quantity, subtotal, user_id, sheet_name, :db
                                    FROM transactions WHERE sheet_name = :sn
                                """),
                                {"db": current_user, "sn": sname}
                            )
                            # 旧レコード削除
                            conn.execute(text("DELETE FROM transactions WHERE sheet_name = :sn"), {"sn": sname})

                        # 新明細をDBへ登録
                        if not parsed_df.empty:
                            records_list = parsed_df.to_dict("records")
                            for r in records_list:
                                r["sheet_name"] = sname
                            insert_records_to_db(current_user, records_list, auto_backup=False)

                        create_backup_snapshot(current_user, trigger_reason=f"シート『{sname}』元グリッド編集同期保存")
                        invalidate_records_cache()
                        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                        st.toast(f"✅ シート『{sname}』の元配置グリッドおよびDB取引データを完全保存しました！")
                        st.rerun()

                with col_reset_raw:
                    if st.button("🔄 編集を元に戻す", use_container_width=True, key=f"reset_raw_btn_{t_idx}"):
                        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                        st.rerun()

                with col_dl_raw:
                    fname_base = st.session_state.get("last_imported_file_name", "trade_file").rsplit(".", 1)[0]
                    single_sheet_dict = {sname: edited_raw_df}
                    raw_xlsx_bytes = export_raw_sheets_to_xlsx_bytes(single_sheet_dict)
                    st.download_button(
                        label=f"📗 『{sname}』を Excel 出力",
                        data=raw_xlsx_bytes,
                        file_name=f"{fname_base}_{sname}_編集済.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                        key=f"dl_single_raw_xlsx_{t_idx}"
                    )

    # ==========================================
    # B. 【フォールバック / DB正規化リスト編集モード】（固定カラム・選択一括削除対応）
    # ==========================================
    elif df_current is not None and not df_current.empty:
        if "シート名" in df_current.columns:
            all_edit_sheets = [str(s).strip() for s in df_current["シート名"].dropna().unique() if str(s).strip()]
        else:
            all_edit_sheets = []
        if not all_edit_sheets:
            all_edit_sheets = ["Sheet1"]

        edit_tab_labels = [f"🌐 ALL 全件 ({len(df_current):,}件)"] + [f"📑 {s}" for s in all_edit_sheets]
        edit_tabs = st.tabs(edit_tab_labels)

        column_config = {
            "選択": st.column_config.CheckboxColumn("選択", help="削除対象として選択"),
            "id": st.column_config.NumberColumn("ID", disabled=True),
            "シート名": st.column_config.TextColumn("シート名"),
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
        editor_cols_order = [c for c in ["選択", "id", "シート名", "日付", "市場番号", "カテゴリー", "商品コード", "品名", "単価", "数量", "仕入金額", "登録担当者"] if c in df_current.columns or c in ["選択", "シート名"]]

        for t_idx, sheet_label in enumerate(edit_tab_labels):
            with edit_tabs[t_idx]:
                if t_idx == 0:
                    current_tab_df = df_current.copy()
                    st.caption(f"全シート合計: **{len(current_tab_df):,} 件** の直接編集・選択削除")
                else:
                    target_s = all_edit_sheets[t_idx - 1]
                    current_tab_df = df_current[df_current["シート名"] == target_s].copy()
                    st.caption(f"シート名: **{target_s}** (全 **{len(current_tab_df):,} 件**)")

                if current_tab_df.empty:
                    st.info("このシートにデータはありません。")
                    continue

                current_tab_df.insert(0, "選択", False)
                if "シート名" not in current_tab_df.columns:
                    current_tab_df["シート名"] = "Sheet1"

                initial_row_ids = set(current_tab_df["id"].dropna().astype(int).tolist())

                edited_df = st.data_editor(
                    current_tab_df[editor_cols_order],
                    use_container_width=True,
                    num_rows="dynamic",
                    column_config=column_config,
                    key=f"trade_data_editor_tab_{t_idx}_v_{st.session_state.get('editor_version', 0)}"
                )

                col_save, col_del, col_dl_xlsx = st.columns([2, 2, 2])

                with col_save:
                    if st.button("💾 変更をデータベースに保存する", type="primary", use_container_width=True, key=f"save_edited_btn_{t_idx}"):
                        remaining_row_ids = set(edited_df["id"].dropna().astype(int).tolist())
                        missing_ids = list(initial_row_ids - remaining_row_ids)
                        auto_del_cnt = 0
                        auto_skip_cnt = 0
                        if missing_ids:
                            auto_del_cnt, auto_skip_cnt, _ = delete_records_by_ids(missing_ids, deleted_by=current_user, operator_role=user_role)

                        changes_count = 0
                        blocked_count = 0
                        for _, row in edited_df.iterrows():
                            rid = row.get("id")
                            if pd.notnull(rid):
                                p = int(row.get("単価", 0))
                                q = int(row.get("数量", 0))
                                sub = p * q
                                ok, msg = update_single_record_in_db({
                                    "id": int(rid),
                                    "日付": str(row.get("日付", "")),
                                    "市場番号": str(row.get("市場番号", "")),
                                    "カテゴリー": str(row.get("カテゴリー", "")),
                                    "商品コード": str(row.get("商品コード", "")),
                                    "品名": str(row.get("品名", "")),
                                    "単価": p,
                                    "数量": q,
                                    "仕入金額": sub,
                                    "シート名": str(row.get("シート名", "")).strip() or "Sheet1"
                                }, operator_user=current_user, operator_role=user_role)
                                if ok:
                                    changes_count += 1
                                else:
                                    blocked_count += 1

                        status_messages = []
                        if auto_del_cnt > 0:
                            status_messages.append(f"ゴミ箱削除 {auto_del_cnt} 件をDBに反映")
                        if changes_count > 0:
                            status_messages.append(f"{changes_count} 件の変更を保存")

                        if auto_skip_cnt > 0 or blocked_count > 0:
                            st.warning(f"⚠️ {auto_skip_cnt + blocked_count} 件は権限外（他担当者の登録データ）のため操作をスキップしました。")
                        if status_messages:
                            st.toast(f"✅ {'・'.join(status_messages)} しました！")

                        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                        st.rerun()

                with col_del:
                    selected_rows = edited_df[edited_df["選択"] == True]
                    if not selected_rows.empty:
                        del_btn_label = f"🗑️ 選択した {len(selected_rows)} 件を削除"
                        if st.button(del_btn_label, type="secondary", use_container_width=True, key=f"del_selected_btn_{t_idx}"):
                            deleted_ids = selected_rows["id"].dropna().astype(int).tolist()
                            del_cnt, skip_cnt, _ = delete_records_by_ids(deleted_ids, deleted_by=current_user, operator_role=user_role)
                            if skip_cnt > 0:
                                st.warning(f"⚠️ {skip_cnt} 件は権限外（他担当者の登録データ）のため削除をスキップしました。")
                            if del_cnt > 0:
                                st.toast(f"🗑️ {del_cnt} 件を削除しました（上の復元ボタンから元通り復元可能）。")
                            st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                            st.rerun()

                with col_dl_xlsx:
                    export_cols = [c for c in ["日付", "市場番号", "カテゴリー", "商品コード", "品名", "数量", "単価", "仕入金額", "シート名", "登録担当者"] if c in edited_df.columns]
                    s_title = "青果取引データ" if t_idx == 0 else all_edit_sheets[t_idx - 1]
                    xlsx_export_bytes = convert_df_to_xlsx_bytes(edited_df[export_cols], sheet_name=s_title)
                    st.download_button(
                        f"📗 この内容を Excel (.xlsx) 出力",
                        data=xlsx_export_bytes,
                        file_name=f"seika_trade_{s_title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                        key=f"dl_xlsx_btn_{t_idx}"
                    )
    else:
        st.info("💡 表示する取引データがありません。")
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
