import streamlit as st
import pandas as pd
from datetime import datetime
import os
import hashlib
import json
import glob
import plotly.express as px
from sqlalchemy import create_engine, text
import io

# ==========================================
# ページ基本設定 & CSS
# ==========================================
st.set_page_config(
    page_title="青果卸売 在庫・取引管理 Pro",
    page_icon="🍎",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
    <style>
    .main { background-color: #f8f9fa; }
    .app-header {
        background: linear-gradient(135deg, #1e7e34 0%, #28a745 100%);
        padding: 1.2rem 1.5rem;
        border-radius: 10px;
        color: white;
        margin-bottom: 1.2rem;
        box-shadow: 0 4px 12px rgba(40, 167, 69, 0.15);
    }
    .app-header h1 { margin: 0; font-size: 1.6rem; font-weight: 700; color: #ffffff; }
    .app-header p { margin: 2px 0 0 0; opacity: 0.9; font-size: 0.9rem; }
    .section-title {
        font-size: 1.1rem;
        font-weight: 700;
        color: #2d3748;
        margin-bottom: 0.8rem;
        border-bottom: 2px solid #e2e8f0;
        padding-bottom: 4px;
    }
    .role-badge-admin { background-color: #dc3545; color: white; padding: 2px 6px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
    .role-badge-staff { background-color: #28a745; color: white; padding: 2px 6px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
    .db-badge-cloud { background-color: #0284c7; color: white; padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
    .db-badge-local { background-color: #4b5563; color: white; padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
    .admin-monitor-card {
        background: #f0fdf4;
        border: 1px solid #bbf7d0;
        padding: 14px 18px;
        border-radius: 10px;
        margin-bottom: 16px;
    }
    .restore-box {
        background-color: #f0fdf4;
        border: 1px solid #86efac;
        padding: 12px 16px;
        border-radius: 8px;
        margin-bottom: 15px;
    }
    .mismatch-box {
        background-color: #fef2f2;
        border: 1px solid #fca5a5;
        padding: 14px 18px;
        border-radius: 8px;
        margin-bottom: 15px;
    }
    div[data-testid="stStatusWidget"] { visibility: hidden; display: none; }
    div[data-st-mode="running"] { opacity: 1 !important; filter: none !important; }
    </style>
""", unsafe_allow_html=True)

# ==========================================
# 認証・セキュリティ設定 & ユーザー管理
# ==========================================
@st.cache_data
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

DEFAULT_USERS = {
    "admin": {"pw": hash_password("password123"), "role": "admin", "name": "管理者 (統括)"},
    "user1": {"pw": hash_password("seika2026"), "role": "staff", "name": "社員A (東京市場担当)"},
    "staff_a": {"pw": hash_password("market01"), "role": "staff", "name": "社員B (横浜市場担当)"},
    "staff_c": {"pw": hash_password("seika03"), "role": "staff", "name": "社員C (大田市場担当)"}
}

if "user_db" not in st.session_state:
    st.session_state.user_db = DEFAULT_USERS.copy()

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

MASTER_CSV_FILE = "item_master.csv"
BACKUP_DIR = "db_backups"
os.makedirs(BACKUP_DIR, exist_ok=True)

# ==========================================
# 1. クラウドDB対応（PostgreSQL / Supabase / SQLite）
# ==========================================
def get_db_url():
    custom_url = st.session_state.get("custom_database_url", "").strip()
    if custom_url:
        return custom_url
    return os.environ.get("DATABASE_URL", "sqlite:///seika_inventory.db")

@st.cache_resource
def get_db_engine(db_url: str):
    clean_url = db_url.strip()
    if clean_url.startswith("postgres://"):
        clean_url = clean_url.replace("postgres://", "postgresql://", 1)
        
    is_sqlite = clean_url.startswith("sqlite")
    
    if is_sqlite:
        engine = create_engine(
            clean_url,
            connect_args={"check_same_thread": False, "timeout": 30.0}
        )
    else:
        engine = create_engine(
            clean_url,
            pool_size=20,
            max_overflow=40,
            pool_recycle=300,
            pool_pre_ping=True
        )
    return engine

def get_current_engine():
    current_url = get_db_url()
    return get_db_engine(current_url)

def get_db_info():
    url = get_db_url()
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
        return {"type": "local", "badge": "📁 ローカル SQLite", "desc": "sqlite:///seika_inventory.db", "is_cloud": False}

def init_db():
    engine = get_current_engine()
    with engine.begin() as conn:
        id_type = "INTEGER PRIMARY KEY AUTOINCREMENT" if "sqlite" in engine.name else "SERIAL PRIMARY KEY"
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
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_records_market ON records(market_no)"))
        except Exception:
            pass

init_db()

# ==========================================
# 2. 自動バックアップ & BCPリストア エンジン
# ==========================================
def create_backup_snapshot(user_id=None, trigger_reason="自動バックアップ"):
    try:
        engine = get_current_engine()
        with engine.connect() as conn:
            df_rec = pd.read_sql_query(text("SELECT * FROM records ORDER BY id ASC"), conn)
            df_del = pd.read_sql_query(text("SELECT * FROM deleted_records ORDER BY id ASC"), conn)

        now = datetime.now()
        ts_str = now.strftime("%Y%m%d_%H%M%S")
        backup_filename = f"seika_backup_{ts_str}.json"
        backup_path = os.path.join(BACKUP_DIR, backup_filename)

        backup_payload = {
            "version": "2.0",
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": trigger_reason,
            "created_by": user_id or "system",
            "records_count": len(df_rec),
            "total_amount": int(df_rec["subtotal"].sum()) if not df_rec.empty and "subtotal" in df_rec.columns else 0,
            "records": df_rec.to_dict("records"),
            "deleted_records": df_del.to_dict("records")
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

        return {"success": True, "count": len(records_to_restore)}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ==========================================
# 3. データCRUD関数（社員別アカウント分離 & 管理者横断確認）
# ==========================================
def load_records_from_db(user_id=None):
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
    return df

def get_employee_summary_stats():
    """管理者向け：社員ごとの入力件数・数量・売上総額の集計サマリー"""
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

def insert_records_to_db(user_id, records_list, auto_backup=True):
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

    if auto_backup:
        create_backup_snapshot(user_id, trigger_reason=f"データ追加 ({len(records_list)}件)")

def delete_records_by_ids(ids_to_delete, user_id=None, auto_backup=True):
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

    if auto_backup:
        create_backup_snapshot(user_id, trigger_reason=f"データ削除 ({len(clean_ids)}件)")
        
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

    create_backup_snapshot(user_id, trigger_reason=f"ゴミ箱からの復元 ({restored_count}件)")
    return restored_count

def purge_deleted_records(user_id=None):
    engine = get_current_engine()
    with engine.begin() as conn:
        if user_id:
            conn.execute(text("DELETE FROM deleted_records WHERE user_id = :user_id"), {"user_id": user_id})
        else:
            conn.execute(text("DELETE FROM deleted_records"))

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

# ==========================================
# 4. マスタデータ管理
# ==========================================
@st.cache_data(ttl=3600)
def load_item_master_cached():
    if not os.path.exists(MASTER_CSV_FILE):
        default_master = pd.DataFrame([
            {"category": "ハーブ・薬味類", "item_code": "10401", "item_name": "大葉"},
            {"category": "ハーブ・薬味類", "item_code": "10402", "item_name": "ミント"},
            {"category": "ハーブ・薬味類", "item_code": "10403", "item_name": "バジル"},
            {"category": "キノコ類", "item_code": "10501", "item_name": "しいたけ"},
            {"category": "キノコ類", "item_code": "10502", "item_name": "えのき"},
            {"category": "野菜", "item_code": "10523", "item_name": "キャベツ"},
            {"category": "野菜", "item_code": "10524", "item_name": "レタス"},
            {"category": "野菜", "item_code": "10525", "item_name": "トマト"},
            {"category": "野菜", "item_code": "10528", "item_name": "人参"},
            {"category": "イモ類・根菜", "item_code": "10601", "item_name": "長いも"},
            {"category": "イモ類・根菜", "item_code": "10602", "item_name": "さつまいも"},
            {"category": "果物", "item_code": "20101", "item_name": "りんご"}
        ])
        default_master.to_csv(MASTER_CSV_FILE, index=False, encoding="utf-8-sig")
    
    df_master = pd.read_csv(MASTER_CSV_FILE, dtype=str)
    master_dict = {}
    for _, row in df_master.iterrows():
        cat, code, name = str(row["category"]), str(row["item_code"]), str(row["item_name"])
        if cat not in master_dict:
            master_dict[cat] = {}
        master_dict[cat][code] = name
    return master_dict, df_master

ITEM_MASTER, df_master_raw = load_item_master_cached()

# ==========================================
# 5. ログイン画面
# ==========================================
if st.session_state.logged_in_user is None:
    st.markdown("""
        <div style="text-align: center; margin-top: 30px;">
            <span style="font-size: 54px;">🍎</span>
            <h1 style="color: #1e7e34; margin-bottom: 4px;">青果卸売 在庫・取引管理 Pro</h1>
            <p style="color: #718096; font-size: 14px;">社員別アカウント個別入力・管理者横断データ監視・BCPクラウドDB完備</p>
        </div>
    """, unsafe_allow_html=True)

    col_l1, col_l2, col_l3 = st.columns([1, 1.2, 1])
    with col_l2:
        with st.container():
            username_input = st.text_input("👤 ユーザーID", placeholder="admin, user1, staff_a, staff_c 等")
            password_input = st.text_input("🔒 パスワード", type="password", placeholder="••••••••")
            
            if st.button("ログインする", use_container_width=True, type="primary"):
                hashed_input = hash_password(password_input)
                user_data = st.session_state.user_db.get(username_input)
                if user_data and user_data["pw"] == hashed_input:
                    st.session_state.logged_in_user = username_input
                    st.session_state.user_role = user_data["role"]
                    create_backup_snapshot(username_input, trigger_reason="ログイン時定期バックアップ")
                    st.rerun()
                else:
                    st.error("IDまたはパスワードが無効です")

            st.write("")
            st.markdown("---")
            st.caption("テスト用クイックログイン（アカウント切り替えテスト）:")
            q1, q2, q3, q4 = st.columns(4)
            with q1:
                if st.button("👑 管理者", use_container_width=True):
                    st.session_state.logged_in_user = "admin"
                    st.session_state.user_role = "admin"
                    st.rerun()
            with q2:
                if st.button("👤 user1", use_container_width=True):
                    st.session_state.logged_in_user = "user1"
                    st.session_state.user_role = "staff"
                    st.rerun()
            with q3:
                if st.button("👤 staff_a", use_container_width=True):
                    st.session_state.logged_in_user = "staff_a"
                    st.session_state.user_role = "staff"
                    st.rerun()
            with q4:
                if st.button("👤 staff_c", use_container_width=True):
                    st.session_state.logged_in_user = "staff_c"
                    st.session_state.user_role = "staff"
                    st.rerun()
    st.stop()

# ログイン中のユーザー情報
current_user = st.session_state.logged_in_user
current_role = st.session_state.user_role
db_status = get_db_info()

# ==========================================
# 6. サイドバー
# ==========================================
with st.sidebar:
    st.image("https://img.icons8.com/emoji/96/000000/red-apple.png", width=50)
    st.title("青果卸売 Pro")
    
    role_badge = '<span class="role-badge-admin">👑 管理者 (全社員閲覧可)</span>' if current_role == "admin" else '<span class="role-badge-staff">👤 社員 (個別データ管理)</span>'
    st.markdown(f"**{current_user}** {role_badge}", unsafe_allow_html=True)

    db_badge_html = f'<span class="db-badge-cloud">{db_status["badge"]}</span>' if db_status["is_cloud"] else f'<span class="db-badge-local">{db_status["badge"]}</span>'
    st.markdown(f"**DB環境**: {db_badge_html}", unsafe_allow_html=True)

    if st.button("ログアウト", use_container_width=True):
        st.session_state.logged_in_user = None
        st.session_state.user_role = None
        st.session_state.records_df = None
        st.session_state.admin_target_user = None
        st.session_state.verification_batch = None
        st.session_state.last_deleted_records = []
        st.rerun()

    if current_role == "admin":
        st.divider()
        with st.expander("👤 社員アカウント追加 (管理者限定)", expanded=False):
            new_u_id = st.text_input("新規社員ID (英数字)", placeholder="例: staff_tanaka")
            new_u_name = st.text_input("社員氏名 / 担当市場", placeholder="例: 田中 (築地第二担当)")
            new_u_pw = st.text_input("パスワード", type="password", placeholder="••••••••")
            new_u_role = st.selectbox("権限ロール", ["staff", "admin"], format_func=lambda x: "一般社員" if x == "staff" else "管理者")
            
            if st.button("➕ 社員アカウントを発行", type="primary", use_container_width=True):
                if new_u_id.strip() and new_u_pw.strip():
                    if new_u_id in st.session_state.user_db:
                        st.error("その社員IDは既に存在します。")
                    else:
                        st.session_state.user_db[new_u_id.strip()] = {
                            "pw": hash_password(new_u_pw),
                            "role": new_u_role,
                            "name": new_u_name.strip() or new_u_id
                        }
                        st.success(f"✅ 社員アカウント 『{new_u_id}』 を登録しました！")
                        st.rerun()
                else:
                    st.warning("IDとパスワードを入力してください")

    st.divider()
    with st.expander("⚙️ クラウドDB接続設定", expanded=False):
        new_db_url = st.text_input(
            "接続文字列 (DATABASE_URL)", 
            value=st.session_state.get("custom_database_url", os.environ.get("DATABASE_URL", "")),
            placeholder="postgresql://postgres:[PASSWORD]@db.xxxx.supabase.co:5432/postgres",
            type="password"
        )
        col_db1, col_db2 = st.columns(2)
        with col_db1:
            if st.button("クラウドDB接続", type="primary", use_container_width=True):
                if new_db_url.strip():
                    st.session_state.custom_database_url = new_db_url.strip()
                    try:
                        init_db()
                        st.session_state.records_df = None
                        st.success("☁️ クラウドDBに接続しました！")
                        st.rerun()
                    except Exception as e:
                        st.error(f"接続エラー: {e}")
        with col_db2:
            if st.button("ローカルに戻す", use_container_width=True):
                st.session_state.custom_database_url = ""
                init_db()
                st.session_state.records_df = None
                st.info("📁 ローカルDBに切り替えました")
                st.rerun()

    if st.button("⚡ 今すぐDBスナップショット作成", use_container_width=True):
        res = create_backup_snapshot(current_user, trigger_reason="手動即時バックアップ")
        if res.get("success"):
            st.success(f"💾 バックアップ作成完了 ({res['count']}件)")

# ==========================================
# 7. 表示対象データの決定
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
        <p>社員別アカウント個別入力・管理者横断データ監視・クラウドDB・BCPバックアップ完備</p>
    </div>
""", unsafe_allow_html=True)

# 管理者用：表示対象社員セレクター
if current_role == "admin":
    st.markdown('<div class="admin-monitor-card">', unsafe_allow_html=True)
    c_m1, c_m2, c_m3 = st.columns([3, 1.5, 1.5])
    with c_m1:
        user_list = list(st.session_state.user_db.keys())
        user_labels = {None: "👥 【全社員統合ビュー】すべての社員のデータを一括表示"}
        for u in user_list:
            u_info = st.session_state.user_db[u]
            u_name = u_info.get("name", u)
            u_role_str = "👑 管理者" if u_info["role"] == "admin" else "👤 社員"
            user_labels[u] = f"{u_role_str}: {u} ({u_name})"

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
            st.rerun()

    with c_m2:
        st.write("")
        if st.session_state.admin_target_user is not None:
            st.info(f"現在 **{st.session_state.admin_target_user}** の個別データを閲覧中")
        else:
            st.success("現在 **全社員のデータ** を統合閲覧中")
    with c_m3:
        st.write("")
        if st.session_state.admin_target_user is not None:
            if st.button("👥 全社員表示に戻す", use_container_width=True):
                st.session_state.admin_target_user = None
                st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

tab_titles = [
    "📝 取引データ入力・管理", 
    "📊 データ分析", 
    "👥 社員別データ監視 (管理者)" if current_role == "admin" else "👤 担当データ状況",
    "🛡️ BCP自動バックアップ・復旧",
    "📥 CSV一括インポート"
]
tabs = st.tabs(tab_titles)

# ==========================================
# タブ 1: 取引データ入力・管理
# ==========================================
with tabs[0]:
    records_cnt = len(df_current) if df_current is not None else 0
    total_qty_sum = df_current["数量"].sum() if records_cnt > 0 else 0
    total_amount_sum = df_current["小計"].sum() if records_cnt > 0 else 0

    k1, k2, k3, k4 = st.columns([1, 1, 1.2, 1.3])
    k1.metric("📋 取引件数", f"{records_cnt:,} 件")
    k2.metric("📦 総取引数量", f"{total_qty_sum:,} 個")
    k3.metric("💰 総取引金額", f"¥ {total_amount_sum:,}")
    
    view_owner_label = f"全社員統合 ({len(st.session_state.user_db)}名)" if viewing_user_filter is None else f"{viewing_user_filter} (専用)"
    k4.metric("👤 表示対象アカウント", view_owner_label)

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
                st.success(f"✅ {len(restored_records)} 件のデータを元通りに復元しました！")
                st.rerun()
        with col_undo2:
            if st.button("✖ 閉じる", use_container_width=False):
                st.session_state.last_deleted_records = []
                st.rerun()

    # 元ファイル照合
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

    col_b1, col_b2 = st.columns(2)
    with col_b1:
        market_no = st.text_input("市場番号 / 取引先データ", value="M-001 (築地第一取引先)")
    with col_b2:
        input_date = st.date_input("取引日付", datetime.now())

    with st.container():
        st.markdown(f"""
            <div style="background-color: #f0fff4; border: 2px dashed #38a169; padding: 12px 16px; border-radius: 8px; margin-bottom: 12px;">
                <b style="color: #22543d;">📁 取引先ファイルを選択して自動記録（アカウント: {current_user} の個別データとして登録）</b><br>
                <small style="color: #4a5568;">ファイルを取り込むと、ログイン中の社員アカウント（{current_user}）専用のデータとして安全に自動記録されます。</small>
            </div>
        """, unsafe_allow_html=True)
        
        col_up1, col_up2 = st.columns([4, 1])
        with col_up1:
            uploaded_entry_file = st.file_uploader(
                "取引データファイルを選択", 
                type=["csv", "txt", "tsv"], 
                key="auto_unique_verified_uploader", 
                label_visibility="collapsed"
            )
        with col_up2:
            if st.button("🧪 サンプルデータ記録", use_container_width=True):
                sample_items = [
                    {"日付": str(input_date), "市場番号": market_no, "カテゴリー": "野菜", "商品コード": "10525", "商品名": "トマト", "単価": 290, "数量": 50, "小計": 14500},
                    {"日付": str(input_date), "市場番号": market_no, "カテゴリー": "野菜", "商品コード": "10523", "商品名": "キャベツ", "単価": 200, "数量": 40, "小計": 8000},
                    {"日付": str(input_date), "市場番号": market_no, "カテゴリー": "果物", "商品コード": "20101", "商品名": "りんご", "単価": 360, "数量": 30, "小計": 10800}
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
                    if "日付" not in df_file.columns: df_file["日付"] = str(input_date)
                    if "市場番号" not in df_file.columns: df_file["市場番号"] = market_no
                    if "カテゴリー" not in df_file.columns: df_file["カテゴリー"] = "野菜"
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

    st.write("")
    st.write(f"▼ 個別商品の手動入力（ログイン社員: **{current_user}** のデータとして登録されます）")
    all_categories = list(ITEM_MASTER.keys()) or ["野菜", "果物", "ハーブ・薬味類", "キノコ類", "イモ類・根菜"]
    form_rows_widgets = []

    for i in range(1, 6):
        c_chk, c_cat, c_item, c_price, c_qty = st.columns([0.8, 2.2, 3.2, 2.0, 2.0])
        with c_chk:
            st.write("")
            is_active = st.checkbox(f"行 {i}", value=(i == 1), key=f"f_check_{i}")
        with c_cat:
            category = st.selectbox(f"カテゴリー #{i}", all_categories, key=f"dyn_cat_{i}", label_visibility="collapsed" if i > 1 else "visible")
        
        items_in_cat = ITEM_MASTER.get(category, {})
        item_options = [f"{code} : {name}" for code, name in items_in_cat.items()]
        
        with c_item:
            selected_item_str = st.selectbox(f"商品選択 #{i}", item_options, key=f"dyn_item_{i}_{category}", label_visibility="collapsed" if i > 1 else "visible") if item_options else ""
        with c_price:
            unit_price = st.number_input(f"単価 #{i}", min_value=0, value=100 if is_active else 0, step=10, key=f"f_price_{i}", label_visibility="collapsed" if i > 1 else "visible")
        with c_qty:
            quantity = st.number_input(f"数量 #{i}", min_value=0, value=1 if is_active else 0, step=1, key=f"f_qty_{i}", label_visibility="collapsed" if i > 1 else "visible")
        
        form_rows_widgets.append({"is_active": is_active, "category": category, "item_str": selected_item_str, "price": unit_price, "qty": quantity})

    if st.button("➕ チェックした手動入力商品をリストに追加保存", type="primary", use_container_width=True, key="save_input_data_btn"):
        if not market_no.strip():
            st.error("❌ 市場番号 / 取引先データを入力してください。")
        else:
            valid_items = []
            for r in form_rows_widgets:
                if r["is_active"] and r["item_str"]:
                    code, name = r["item_str"].split(" : ")
                    if r["price"] > 0 and r["qty"] > 0:
                        valid_items.append({
                            "日付": input_date.strftime("%Y-%m-%d"),
                            "市場番号": market_no,
                            "カテゴリー": r["category"],
                            "商品コード": code,
                            "商品名": name,
                            "単価": r["price"],
                            "数量": r["qty"],
                            "小計": r["price"] * r["qty"]
                        })
            if valid_items:
                insert_records_to_db(current_user, valid_items)
                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.toast(f"✅ {len(valid_items)} 件を登録しました！")
                st.rerun()

    st.write("")
    st.markdown('<div class="section-title">2️⃣ 登録済みデータ一覧・編集・安全な削除（いつでも復元可能）</div>', unsafe_allow_html=True)

    if records_cnt > 0:
        st.info("💡 **操作方法:** セルを直接編集して「💾 編集内容を保存」を押すか、行を削除して「🗑️ 選択・削除された行を反映する」を押してください。")
        
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
            height=360,
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

                create_backup_snapshot(current_user, trigger_reason="データ編集・保存")
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
                label="📥 表示中のCSVデータをダウンロード",
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
            fig_pie.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=280)
            st.plotly_chart(fig_pie, use_container_width=True)
            
        with g_col2:
            st.subheader("📈 日別売上金額推移")
            df_daily = df_ana.groupby("日付")["小計"].sum().reset_index()
            fig_line = px.line(df_daily, x="日付", y="小計", markers=True)
            fig_line.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=280)
            st.plotly_chart(fig_line, use_container_width=True)
    else:
        st.info("💡 分析対象データがありません。")

# ==========================================
# タブ 3: ★【新機能】社員別データ監視・確認センター（管理者専用）★
# ==========================================
with tabs[2]:
    if current_role == "admin":
        st.markdown('<div class="section-title">👥 管理者用：社員別データ入力状況・横断モニタリング</div>', unsafe_allow_html=True)
        st.markdown("""
            全社員のアカウントごとに、**どのアカウントが、どれだけの件数・数量・売上データを入力しているか** をリアルタイムで監視・集計・比較できます。
        """)

        df_summary = get_employee_summary_stats()
        
        if not df_summary.empty:
            df_summary["社員名/担当"] = df_summary["社員ID"].apply(
                lambda uid: st.session_state.user_db.get(uid, {}).get("name", uid)
            )
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
                fig_bar.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=300, showlegend=False)
                st.plotly_chart(fig_bar, use_container_width=True)

            with c_g2:
                st.subheader("🍩 社員別 入力件数シェア")
                fig_pie_staff = px.pie(
                    df_summary, 
                    names="社員名/担当", 
                    values="登録件数", 
                    hole=0.45
                )
                fig_pie_staff.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=300)
                st.plotly_chart(fig_pie_staff, use_container_width=True)

            st.divider()
            st.subheader("🔍 社員を指定して登録データを明細確認")
            picked_staff = st.selectbox(
                "明細を確認する社員を選択",
                options=df_summary["社員ID"].tolist(),
                format_func=lambda uid: f"{uid} ({st.session_state.user_db.get(uid, {}).get('name', uid)})"
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
        st.info(f"ログイン中のアカウント: **{current_user}** （一般社員）\n\nあなたのアカウントで入力したデータは安全に個別に保存されています。他の社員のデータと混ざることはありません。")
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
    st.markdown('<div class="section-title">🛡️ 企業のBCP対策：自動バックアップ履歴 & データ復旧（リストア）</div>', unsafe_allow_html=True)
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
