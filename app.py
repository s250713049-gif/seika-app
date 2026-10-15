import streamlit as st
import pandas as pd
from datetime import datetime
import os
import hashlib
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
# 認証・セキュリティ設定
# ==========================================
@st.cache_data
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

DEFAULT_USERS = {
    "admin": {"pw": hash_password("password123"), "role": "admin"},
    "user1": {"pw": hash_password("seika2026"), "role": "staff"},
    "staff_a": {"pw": hash_password("market01"), "role": "staff"}
}

if "user_db" not in st.session_state:
    st.session_state.user_db = DEFAULT_USERS.copy()

# 初回アクセス時は未ログイン状態（None）にしてログイン画面を強制表示
if "logged_in_user" not in st.session_state:
    st.session_state.logged_in_user = None
    st.session_state.user_role = None

if "processed_file_hashes" not in st.session_state:
    st.session_state.processed_file_hashes = set()

# 照合結果保持用ステート
if "verification_batch" not in st.session_state:
    st.session_state.verification_batch = None

# 直前に削除したデータのUndo用保持ステート
if "last_deleted_records" not in st.session_state:
    st.session_state.last_deleted_records = []

MASTER_CSV_FILE = "item_master.csv"

# ==========================================
# 1. DB接続設定 & 初期化
# ==========================================
@st.cache_resource
def get_db_engine():
    db_url = os.environ.get("DATABASE_URL", "sqlite:///seika_inventory.db")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
        
    if db_url.startswith("sqlite"):
        engine = create_engine(db_url, connect_args={"check_same_thread": False, "timeout": 15.0})
    else:
        engine = create_engine(db_url, pool_size=10, max_overflow=20, pool_pre_ping=True)
    return engine

engine = get_db_engine()

def init_db():
    with engine.begin() as conn:
        id_type = "INTEGER PRIMARY KEY AUTOINCREMENT" if "sqlite" in engine.name else "SERIAL PRIMARY KEY"
        # 現行の取引レコードテーブル
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
        # ★【新機能】削除データ退避・復元用テーブル（ゴミ箱）★
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

init_db()

# ==========================================
# 2. データCRUD関数 & 復元（Restore）関数
# ==========================================
def load_records_from_db(user_id=None):
    with engine.connect() as conn:
        if user_id is None:
            query = text("SELECT id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal FROM records ORDER BY id ASC")
            df = pd.read_sql_query(query, conn)
        else:
            query = text("SELECT id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal FROM records WHERE user_id = :user_id ORDER BY id ASC")
            df = pd.read_sql_query(query, conn, params={"user_id": user_id})
    
    if not df.empty:
        df["id"] = df["id"].astype(int)
        df = df.rename(columns={
            "id": "id",
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

def insert_records_to_db(user_id, records_list):
    """レコードをDBの最下部に新規追加登録する"""
    if not records_list:
        return []
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
                
            params.append({
                "user_id": user_id,
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

def delete_records_by_ids(ids_to_delete, user_id=None):
    """
    ★【新機能】データを完全消去せず、deleted_records（ゴミ箱）に退避保存してから削除する仕様
    これにより、いつでも元通りに復元可能！
    """
    if not ids_to_delete:
        return []
    clean_ids = [int(i) for i in ids_to_delete if pd.notnull(i) and str(i).replace('.0','').isdigit()]
    if not clean_ids:
        return []
        
    backed_up_rows = []
    with engine.begin() as conn:
        id_str = ",".join(map(str, clean_ids))
        # 1. 削除対象データを取得してバックアップ用に確保
        fetch_query = text(f"SELECT id, user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal FROM records WHERE id IN ({id_str})")
        res = conn.execute(fetch_query).fetchall()
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for row in res:
            backed_up_rows.append(dict(row._mapping))
            # 2. ゴミ箱（deleted_records）テーブルに挿入
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
            
        # 3. 現行テーブルから削除
        conn.execute(text(f"DELETE FROM records WHERE id IN ({id_str})"))
        
    return backed_up_rows

# ★【新機能】削除済みデータ（ゴミ箱）の読み込み
def load_deleted_records_from_db(user_id=None):
    with engine.connect() as conn:
        if user_id is None:
            query = text("SELECT id, original_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal, deleted_at FROM deleted_records ORDER BY id DESC")
            df = pd.read_sql_query(query, conn)
        else:
            query = text("SELECT id, original_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal, deleted_at FROM deleted_records WHERE user_id = :user_id ORDER BY id DESC")
            df = pd.read_sql_query(query, conn, params={"user_id": user_id})
    if not df.empty:
        df["id"] = df["id"].astype(int)
        df = df.rename(columns={
            "id": "ゴミ箱ID",
            "original_id": "元ID",
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

# ★【新機能】ゴミ箱から元のrecordsテーブルへ完全復元する関数
def restore_records_from_trash(deleted_ids_to_restore, user_id):
    """指定されたゴミ箱レコードを元のリストに完全復元する"""
    if not deleted_ids_to_restore:
        return 0
    clean_ids = [int(i) for i in deleted_ids_to_restore if pd.notnull(i) and str(i).replace('.0','').isdigit()]
    if not clean_ids:
        return 0
        
    restored_count = 0
    with engine.begin() as conn:
        id_str = ",".join(map(str, clean_ids))
        fetch_query = text(f"SELECT date, market_no, category, item_code, item_name, unit_price, quantity, subtotal FROM deleted_records WHERE id IN ({id_str})")
        rows = conn.execute(fetch_query).fetchall()
        
        insert_query = text('''
            INSERT INTO records (user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal)
            VALUES (:user_id, :date, :market_no, :category, :item_code, :item_name, :unit_price, :quantity, :subtotal)
        ''')
        for r in rows:
            m = r._mapping
            conn.execute(insert_query, {
                "user_id": user_id,
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
            
        # 復元したデータをゴミ箱テーブルから削除
        conn.execute(text(f"DELETE FROM deleted_records WHERE id IN ({id_str})"))
        
    return restored_count

def purge_deleted_records(user_id=None):
    """ゴミ箱を完全に空にする"""
    with engine.begin() as conn:
        if user_id:
            conn.execute(text("DELETE FROM deleted_records WHERE user_id = :user_id"), {"user_id": user_id})
        else:
            conn.execute(text("DELETE FROM deleted_records"))

def cleanup_duplicate_records(user_id):
    with engine.begin() as conn:
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
        return result.rowcount

def update_single_record_in_db(record_dict):
    if "id" not in record_dict or pd.isnull(record_dict["id"]):
        return
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
# 3. マスタデータ管理
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
# 4. ログイン画面（未ログイン時は全画面で表示）
# ==========================================
if st.session_state.logged_in_user is None:
    st.markdown("""
        <div style="text-align: center; margin-top: 30px;">
            <span style="font-size: 54px;">🍎</span>
            <h1 style="color: #1e7e34; margin-bottom: 4px;">青果卸売 在庫・取引管理 Pro</h1>
            <p style="color: #718096; font-size: 14px;">システムへログインしてください</p>
        </div>
    """, unsafe_allow_html=True)

    col_l1, col_l2, col_l3 = st.columns([1, 1.2, 1])
    with col_l2:
        with st.container():
            username_input = st.text_input("👤 ユーザーID", placeholder="admin または 社員ID")
            password_input = st.text_input("🔒 パスワード", type="password", placeholder="••••••••")
            
            if st.button("ログインする", use_container_width=True, type="primary"):
                hashed_input = hash_password(password_input)
                user_data = st.session_state.user_db.get(username_input)
                if user_data and user_data["pw"] == hashed_input:
                    st.session_state.logged_in_user = username_input
                    st.session_state.user_role = user_data["role"]
                    st.rerun()
                else:
                    st.error("IDまたはパスワードが無効です")

            st.write("")
            st.markdown("---")
            st.caption("テスト用クイックログイン:")
            q1, q2, q3 = st.columns(3)
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
    st.stop()

# ログイン中のユーザー情報を取得
current_user = st.session_state.logged_in_user
current_role = st.session_state.user_role

# ==========================================
# 5. サイドバー
# ==========================================
with st.sidebar:
    st.image("https://img.icons8.com/emoji/96/000000/red-apple.png", width=50)
    st.title("青果卸売 Pro")
    
    role_badge = '<span class="role-badge-admin">👑 管理者</span>' if current_role == "admin" else '<span class="role-badge-staff">👤 一般社員</span>'
    st.markdown(f"**{current_user}** {role_badge}", unsafe_allow_html=True)
    if st.button("ログアウト", use_container_width=True):
        st.session_state.logged_in_user = None
        st.session_state.user_role = None
        st.session_state.records_df = None
        st.session_state.verification_batch = None
        st.session_state.last_deleted_records = []
        st.rerun()

    st.divider()
    if st.button("🧹 増えすぎた重複データを掃除する", use_container_width=True):
        deleted_count = cleanup_duplicate_records(current_user)
        st.session_state.records_df = load_records_from_db(current_user)
        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
        st.success(f"重複していた {deleted_count} 件のデータを削除して1件にまとめました！")
        st.rerun()

# ==========================================
# 6. ユーザーデータ読み込み
# ==========================================
if "records_df" not in st.session_state or st.session_state.get("current_loaded_user") != current_user:
    st.session_state.records_df = load_records_from_db(current_user)
    st.session_state.current_loaded_user = current_user
    st.session_state.editor_version = 0

df_current = st.session_state.records_df

# ==========================================
# メインコンテンツエリア
# ==========================================
st.markdown("""
    <div class="app-header">
        <h1>🍎 青果卸売 在庫・取引管理 Pro</h1>
        <p>リアルタイム取引登録・元ファイル自動照合・不一致修正自動追記・削除データ元通り復元プラットフォーム</p>
    </div>
""", unsafe_allow_html=True)

tab_input, tab_analytics, tab_csv_import = st.tabs([
    "📝 取引データ入力・管理 (自動記録・照合・復元対応)", 
    "📊 データ分析", 
    "📥 CSV一括インポート"
])

# ==========================================
# タブ 1: 取引データ入力・管理 & 整合性照合・不一致修正自動追記・削除復元
# ==========================================
with tab_input:
    records_cnt = len(df_current) if df_current is not None else 0
    total_qty_sum = df_current["数量"].sum() if records_cnt > 0 else 0
    total_amount_sum = df_current["小計"].sum() if records_cnt > 0 else 0

    k1, k2, k3 = st.columns(3)
    k1.metric("📋 登録取引件数", f"{records_cnt:,} 件")
    k2.metric("📦 総取引数量", f"{total_qty_sum:,} 個")
    k3.metric("💰 総取引金額", f"¥ {total_amount_sum:,}")

    # ★【新機能】直前に削除したデータがある場合の即時「元通りに復元する（Undo）」バナー★
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
                st.session_state.records_df = load_records_from_db(current_user)
                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.success(f"✅ {len(restored_records)} 件のデータを元通りに復元しました！")
                st.rerun()
        with col_undo2:
            if st.button("✖ 閉じる", use_container_width=False):
                st.session_state.last_deleted_records = []
                st.rerun()

    # ★【新機能】元ファイルとの整合性・完全一致照合 ＆ 不一致箇所の修正と「下に自動的に記録される」機能★
    if st.session_state.verification_batch is not None:
        v_batch = st.session_state.verification_batch
        v_file_name = v_batch["file_name"]
        expected_df = v_batch["df"]
        
        # 現在のDBレコードと照合
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
                        新たに読み込んだ元ファイルのデータと登録内容に食い違いがあります。下記で修正した上で、その修正データを<b>下に自動的に記録</b>することができます。
                    </p>
                </div>
            """, unsafe_allow_html=True)
            
            diff_display_df = pd.DataFrame(mismatches)[["行番号", "商品名", "不一致項目", "元ファイルの値(正)", "現在の登録値(誤)"]]
            st.dataframe(diff_display_df, use_container_width=True)

            # ★【新機能】不一致データを修正し、下に自動的に追記記録するフォーム★
            st.write("▼ **不一致データを修正し、リストの一番下に自動追記記録する**")
            
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
                # ★修正したデータを最下部に自動的に追記記録する実行ボタン★
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
                    # リストの一番下（DBの末尾）に新規自動記録
                    insert_records_to_db(current_user, fixed_record)
                    
                    # 以前の不一致レコードが存在する場合は、ゴミ箱へ退避して差し替え
                    if target_mismatch.get("record_id"):
                        delete_records_by_ids([target_mismatch["record_id"]], current_user)
                    
                    st.session_state.records_df = load_records_from_db(current_user)
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.toast(f"✨ 『{target_mismatch['商品名']}』の修正データをリストの最下部に自動記録しました！")
                    st.rerun()

            with col_act2:
                # ワンクリックで元ファイルの値に合わせて全件修正し下に自動記録
                if st.button("🛠️ 全不一致を元ファイル通りに修正して自動記録", use_container_width=True, key="fix_all_mismatch_btn"):
                    insert_records_to_db(current_user, expected_df.to_dict("records"))
                    cleanup_duplicate_records(current_user)
                    st.session_state.records_df = load_records_from_db(current_user)
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.toast("✅ 元ファイルの内容ですべて修正し、下に自動記録・同期完了しました！")
                    st.rerun()

    st.write("")
    st.markdown('<div class="section-title">1️⃣ 新規取引データの入力 & ファイル自動記録・照合</div>', unsafe_allow_html=True)

    col_b1, col_b2 = st.columns(2)
    with col_b1:
        market_no = st.text_input("市場番号 / 取引先データ", value="M-001 (築地第一取引先)")
    with col_b2:
        input_date = st.date_input("取引日付", datetime.now())

    with st.container():
        st.markdown("""
            <div style="background-color: #f0fff4; border: 2px dashed #38a169; padding: 12px 16px; border-radius: 8px; margin-bottom: 12px;">
                <b style="color: #22543d;">📁 取引先ファイルを選択して自動記録（元データとの自動照合・検証付き）</b><br>
                <small style="color: #4a5568;">ファイルをアップロードすると重複せず1度だけ自動記録され、元の内容と100%一致しているか直ちに自動照合されます。</small>
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
                st.session_state.records_df = load_records_from_db(current_user)
                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.toast(f"✅ テストデータ 3件 を記録・照合しました！")
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
                        st.session_state.records_df = load_records_from_db(current_user)
                        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                        st.toast(f"✅ 『{uploaded_entry_file.name}』から {len(new_records_list)} 件を自動記録・照合完了しました！")
                        st.rerun()
                except Exception as e:
                    st.error(f"ファイル解析エラー: {e}")

    st.write("")
    st.write("▼ 個別商品の手動入力（最大5行）")
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
                st.session_state.records_df = load_records_from_db(current_user)
                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.toast(f"✅ {len(valid_items)} 件を登録しました！")
                st.rerun()

    st.write("")
    st.markdown('<div class="section-title">2️⃣ 登録済みデータ一覧・編集・安全な削除（いつでも復元可能）</div>', unsafe_allow_html=True)

    if records_cnt > 0:
        st.info("💡 **操作方法:** セルを直接編集して「💾 編集内容を保存」を押すか、行を削除して「🗑️ 選択・削除された行を反映する」を押してください。削除したデータはいつでも下のゴミ箱から元通り復元できます。")
        
        edited_df = st.data_editor(
            df_current,
            column_config={
                "id": None,
                "日付": st.column_config.TextColumn("日付", required=True),
                "市場番号": st.column_config.TextColumn("市場番号", required=True),
                "カテゴリー": st.column_config.TextColumn("カテゴリー"),
                "商品コード": st.column_config.TextColumn("商品コード"),
                "商品名": st.column_config.TextColumn("商品名"),
                "単価": st.column_config.NumberColumn("単価 (円)", min_value=0, format="¥%d"),
                "数量": st.column_config.NumberColumn("数量", min_value=0, format="%d"),
                "小計": st.column_config.NumberColumn("小計 (自動計算)", disabled=True, format="¥%d")
            },
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
                
                # ★削除されたデータは退避してゴミ箱に保管（復元可能）
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
                        insert_records_to_db(current_user, [r_dict])

                st.session_state.records_df = load_records_from_db(current_user)
                st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                st.toast("✅ 編集内容を保存しました！")
                st.rerun()

        with col_del:
            if st.button("🗑️ 選択・削除された行を反映する", use_container_width=True):
                existing_ids = set(int(i) for i in df_current["id"].dropna().tolist())
                current_ids = set(int(i) for i in edited_df["id"].dropna().tolist() if pd.notnull(i))
                deleted_ids = existing_ids - current_ids
                
                if deleted_ids:
                    # ★削除時にゴミ箱へ保管（復元可能にする）★
                    backed = delete_records_by_ids(list(deleted_ids), current_user)
                    st.session_state.last_deleted_records = backed
                    st.session_state.records_df = load_records_from_db(current_user)
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.toast(f"🗑️ {len(deleted_ids)} 件のデータを削除しました（復元可能です）")
                    st.rerun()
                else:
                    st.warning("⚠️ テーブル上で行を削除してから押してください。")

        with col_dl:
            csv_data = df_current.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                label="📥 CSVデータダウンロード",
                data=csv_data,
                file_name=f"青果取引データ_{current_user}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=True
            )
    else:
        st.info("💡 登録されたデータはありません。上のフォームから商品を追加するか、ファイルをアップロードしてください。")

    # ==========================================
    # ★【新機能】3️⃣ 削除済みデータ保管庫（ゴミ箱） & 元通り復元システム★
    # ==========================================
    st.write("")
    with st.expander("♻️ 削除したデータの確認・元通り復元（ゴミ箱）", expanded=False):
        df_deleted = load_deleted_records_from_db(current_user)
        deleted_count = len(df_deleted) if df_deleted is not None else 0
        
        st.markdown(f"現在、ゴミ箱には **{deleted_count} 件** の削除データが保管されています。誤って削除したデータはいつでも元通りに復活させることができます。")
        
        if deleted_count > 0:
            c_res_all, c_res_sel, c_purge = st.columns([2, 2.5, 2])
            with c_res_all:
                if st.button("♻️ すべてのデータを元通り復元する", type="primary", use_container_width=True, key="restore_all_trash_btn"):
                    all_del_ids = df_deleted["ゴミ箱ID"].tolist()
                    cnt = restore_records_from_trash(all_del_ids, current_user)
                    st.session_state.records_df = load_records_from_db(current_user)
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.success(f"🎉 {cnt} 件のデータを元通り復元しました！")
                    st.rerun()
            
            with c_purge:
                if st.button("🧹 ゴミ箱を完全に空にする", use_container_width=True, key="purge_trash_btn"):
                    purge_deleted_records(current_user)
                    st.toast("ゴミ箱を完全に空にしました。")
                    st.rerun()

            st.write("▼ 削除済みデータ一覧（復元したい行のIDを確認して復元できます）")
            st.dataframe(df_deleted, use_container_width=True)

            st.write("▼ **指定した行を元通り復元**")
            col_sel1, col_sel2 = st.columns([3, 1.5])
            with col_sel1:
                trash_select_options = [f"ID {row['ゴミ箱ID']}: {row['日付']} | {row['商品名']} (数量: {row['数量']}, 小計: ¥{row['小計']:,}) [削除: {row['削除日時']}]" for _, row in df_deleted.iterrows()]
                selected_trash_idx = st.selectbox("復元するレコードを選択", range(len(trash_select_options)), format_func=lambda i: trash_select_options[i])
            with col_sel2:
                st.write("")
                if st.button("♻️ 選択行を元通り復元", type="primary", use_container_width=True, key="restore_single_btn"):
                    target_trash_id = df_deleted.iloc[selected_trash_idx]["ゴミ箱ID"]
                    restore_records_from_trash([target_trash_id], current_user)
                    st.session_state.records_df = load_records_from_db(current_user)
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.success("✅ 選択したデータを元通り復元しました！")
                    st.rerun()
        else:
            st.caption("現在ゴミ箱は空です。削除されたデータはありません。")

# ==========================================
# タブ 2: データ分析
# ==========================================
with tab_analytics:
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
# タブ 3: CSV一括インポート
# ==========================================
with tab_csv_import:
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
                    st.session_state.records_df = load_records_from_db(current_user)
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.success("インポート完了しました！")
                    st.rerun()
        except Exception as e:
            st.error(f"エラー: {e}")
