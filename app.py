import streamlit as st
import pandas as pd
from datetime import datetime
import os
import hashlib
import plotly.express as px
from sqlalchemy import create_engine, text

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

MASTER_CSV_FILE = "item_master.csv"

# ==========================================
# 1. DB接続設定
# ==========================================
@st.cache_resource
def get_db_engine():
    db_url = os.environ.get("DATABASE_URL", "sqlite:///seika_inventory.db")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
        
    if db_url.startswith("sqlite"):
        engine = create_engine(db_url, connect_args={"check_same_thread": False, "timeout": 10.0})
    else:
        engine = create_engine(db_url, pool_size=10, max_overflow=20, pool_pre_ping=True)
    return engine

engine = get_db_engine()

# ==========================================
# 2. DB初期化
# ==========================================
def init_db():
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

init_db()

# ==========================================
# 3. データCRUD関数
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
    if not records_list:
        return
    with engine.begin() as conn:
        insert_query = text('''
            INSERT INTO records (user_id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal)
            VALUES (:user_id, :date, :market_no, :category, :item_code, :item_name, :unit_price, :quantity, :subtotal)
        ''')
        params = []
        for r in records_list:
            p = int(r.get("単価", 0)) if pd.notnull(r.get("単価")) else 0
            q = int(r.get("数量", 0)) if pd.notnull(r.get("数量")) else 0
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

def delete_records_by_ids(ids_to_delete):
    if not ids_to_delete:
        return
    clean_ids = [int(i) for i in ids_to_delete if pd.notnull(i) and str(i).replace('.0','').isdigit()]
    if clean_ids:
        with engine.begin() as conn:
            id_str = ",".join(map(str, clean_ids))
            conn.execute(text(f"DELETE FROM records WHERE id IN ({id_str})"))

def update_single_record_in_db(record_dict):
    if "id" not in record_dict or pd.isnull(record_dict["id"]):
        return
    with engine.begin() as conn:
        p = int(record_dict.get("単価", 0))
        q = int(record_dict.get("数量", 0))
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

def save_item_master(df_master):
    df_master[["category", "item_code", "item_name"]].to_csv(MASTER_CSV_FILE, index=False, encoding="utf-8-sig")
    st.cache_data.clear()

ITEM_MASTER, df_master_raw = load_item_master_cached()

# ==========================================
# 5. HTML帳票生成
# ==========================================
@st.cache_data
def generate_invoice_html(doc_type, client_name, market_no, target_date, records_tuple):
    records_list = [dict(r) for r in records_tuple]
    total_amount = sum(r["小計"] for r in records_list)
    tax_amount = int(total_amount * 0.1)
    grand_total = total_amount + tax_amount

    rows_html = "".join([
        f"""
        <tr>
            <td style="text-align:center; padding: 6px; border: 1px solid #dee2e6;">{idx}</td>
            <td style="padding: 6px; border: 1px solid #dee2e6;">{r['商品コード']}</td>
            <td style="padding: 6px; border: 1px solid #dee2e6;">{r['商品名']}</td>
            <td style="text-align:right; padding: 6px; border: 1px solid #dee2e6;">{r['数量']:,}</td>
            <td style="text-align:right; padding: 6px; border: 1px solid #dee2e6;">¥{r['単価']:,}</td>
            <td style="text-align:right; padding: 6px; border: 1px solid #dee2e6;">¥{r['小計']:,}</td>
        </tr>
        """ for idx, r in enumerate(records_list, 1)
    ])

    return f"""
    <!DOCTYPE html><html><head><meta charset="utf-8">
    <style>
        body {{ font-family: sans-serif; color: #212529; padding: 20px; }}
        .header {{ text-align: center; border-bottom: 2px solid #28a745; margin-bottom: 20px; }}
        .title {{ font-size: 22px; font-weight: bold; color: #1e7e34; }}
        table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
        th {{ background: #f8f9fa; border: 1px solid #dee2e6; padding: 8px; font-size: 13px; }}
        .total-box {{ float: right; width: 280px; border: 2px solid #28a745; padding: 10px; background: #f8fff9; font-weight: bold; }}
    </style></head><body>
    <div class="header"><div class="title">{doc_type}</div></div>
    <table><tr>
        <td><h2>{client_name} 御中</h2><p>市場番号: <strong>{market_no}</strong><br>発行日: {datetime.now().strftime('%Y年%m月%d日')}<br>対象日: {target_date}</p></td>
        <td style="text-align:right;"><h3>青果卸売管理システム</h3><p>〒104-0045 東京都中央区築地 1-1-1</p></td>
    </tr></table>
    <table><thead><tr><th>No.</th><th>商品コード</th><th>品名</th><th>数量</th><th>単価</th><th>金額</th></tr></thead>
    <tbody>{rows_html}</tbody></table>
    <div class="total-box">小計: ¥{total_amount:,}<br>消費税 (10%): ¥{tax_amount:,}<br><span style="font-size:16px; color:#1e7e34;">総合計: ¥{grand_total:,}</span></div>
    </body></html>
    """

# ==========================================
# 6. サイドバー＆ユーザー認証
# ==========================================
with st.sidebar:
    st.image("https://img.icons8.com/emoji/96/000000/red-apple.png", width=50)
    st.title("青果卸売 Pro")
    
    if "logged_in_user" not in st.session_state:
        st.session_state.logged_in_user = None
        st.session_state.user_role = None

    if st.session_state.logged_in_user is None:
        st.subheader("🔐 ログイン")
        username_input = st.text_input("ユーザーID", placeholder="admin または 社員ID")
        password_input = st.text_input("パスワード", type="password", placeholder="••••••••")
        
        if st.button("ログインする", use_container_width=True, type="primary"):
            hashed_input = hash_password(password_input)
            user_data = st.session_state.user_db.get(username_input)
            if user_data and user_data["pw"] == hashed_input:
                st.session_state.logged_in_user = username_input
                st.session_state.user_role = user_data["role"]
                st.rerun()
            else:
                st.error("IDまたはパスワードが無効です")
        st.stop()
    else:
        current_user = st.session_state.logged_in_user
        current_role = st.session_state.user_role
        role_badge = '<span class="role-badge-admin">👑 管理者</span>' if current_role == "admin" else '<span class="role-badge-staff">👤 一般社員</span>'
        st.markdown(f"**{current_user}** {role_badge}", unsafe_allow_html=True)
        if st.button("ログアウト", use_container_width=True):
            st.session_state.logged_in_user = None
            st.session_state.user_role = None
            st.session_state.records_df = None
            st.rerun()

    st.divider()
    if current_role == "admin":
        with st.expander("👑 管理者設定", expanded=False):
            new_u_id = st.text_input("新規ユーザーID")
            new_u_pw = st.text_input("初期パスワード", type="password")
            new_u_role = st.selectbox("権限", ["staff", "admin"])
            if st.button("アカウント作成", use_container_width=True):
                if new_u_id and new_u_pw:
                    st.session_state.user_db[new_u_id] = {"pw": hash_password(new_u_pw), "role": new_u_role}
                    st.success(f"『{new_u_id}』作成完了")

    with st.expander("📦 商品マスタ追加", expanded=False):
        new_cat = st.text_input("カテゴリー", value="野菜")
        new_code = st.text_input("商品コード")
        new_name = st.text_input("商品名")
        if st.button("マスタ登録", use_container_width=True):
            if new_cat and new_code and new_name:
                new_row = pd.DataFrame([{"category": new_cat, "item_code": new_code, "item_name": new_name}])
                updated_master_df = pd.concat([df_master_raw, new_row], ignore_index=True).drop_duplicates(subset=["item_code"])
                save_item_master(updated_master_df)
                ITEM_MASTER, df_master_raw = load_item_master_cached()
                st.success("マスタ追加成功！")
                st.rerun()

# ==========================================
# 7. ユーザーデータ読み込み
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
        <p>リアルタイム取引登録・データ編集・印刷機能プラットフォーム</p>
    </div>
""", unsafe_allow_html=True)

tab_input, tab_analytics, tab_csv_import, tab_print = st.tabs([
    "📝 取引データ入力・管理", 
    "📊 データ分析", 
    "📥 CSVインポート", 
    "🖨️ 納品書・請求書発行"
])

# ==========================================
# タブ 1: 取引データ入力・管理
# ==========================================
with tab_input:
    records_cnt = len(df_current) if df_current is not None else 0
    total_qty_sum = df_current["数量"].sum() if records_cnt > 0 else 0
    total_amount_sum = df_current["小計"].sum() if records_cnt > 0 else 0

    k1, k2, k3 = st.columns(3)
    k1.metric("📋 登録取引件数", f"{records_cnt:,} 件")
    k2.metric("📦 総取引数量", f"{total_qty_sum:,} 個")
    k3.metric("💰 総取引金額", f"¥ {total_amount_sum:,}")

    st.write("")
    st.markdown('<div class="section-title">1️⃣ 新規取引データの入力</div>', unsafe_allow_html=True)

    col_b1, col_b2 = st.columns(2)
    with col_b1:
        market_no = st.text_input("市場番号", value="M-001")
    with col_b2:
        input_date = st.date_input("取引日付", datetime.now())

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

    if st.button("➕ チェックした商品をリストに追加保存", type="primary", use_container_width=True, key="save_input_data_btn"):
        if not market_no.strip():
            st.error("❌ 市場番号を入力してください。")
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
                st.session_state.editor_version += 1
                st.toast(f"✅ {len(valid_items)} 件を登録しました！")
                st.rerun()

    st.write("")
    st.markdown('<div class="section-title">2️⃣ 登録済みデータ一覧・編集・一括削除</div>', unsafe_allow_html=True)

    if records_cnt > 0:
        st.info("💡 **操作方法:** セルを直接編集して「💾 編集内容を保存」を押すか、一番左のヘッダー/行選択でチェックを入れて「🗑️ 選択した行を削除」を押してください。")
        
        # 編集・選択用テーブル
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
                
                if deleted_ids:
                    delete_records_by_ids(list(deleted_ids))
                
                for _, row in edited_df.iterrows():
                    r_dict = row.to_dict()
                    r_dict["単価"] = int(r_dict.get("単価", 0)) if pd.notnull(r_dict.get("単価")) else 0
                    r_dict["数量"] = int(r_dict.get("数量", 0)) if pd.notnull(r_dict.get("数量")) else 0
                    r_dict["小計"] = r_dict["単価"] * r_dict["数量"]
                    
                    if pd.notnull(r_dict.get("id")):
                        update_single_record_in_db(r_dict)
                    else:
                        insert_records_to_db(current_user, [r_dict])

                st.session_state.records_df = load_records_from_db(current_user)
                st.session_state.editor_version += 1
                st.toast("✅ 編集内容を保存しました！")
                st.rerun()

        with col_del:
            if st.button("🗑️ 選択・削除された行を反映する", use_container_width=True):
                existing_ids = set(int(i) for i in df_current["id"].dropna().tolist())
                current_ids = set(int(i) for i in edited_df["id"].dropna().tolist() if pd.notnull(i))
                deleted_ids = existing_ids - current_ids
                
                if deleted_ids:
                    delete_records_by_ids(list(deleted_ids))
                    st.session_state.records_df = load_records_from_db(current_user)
                    st.session_state.editor_version += 1
                    st.toast(f"🗑️ {len(deleted_ids)} 件のデータを削除しました！")
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
        st.info("💡 登録されたデータはありません。上のフォームから商品を追加してください。")

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
    uploaded_file = st.file_uploader("取引データCSVを選択", type=["csv"])
    if uploaded_file is not None:
        try:
            df_up = pd.read_csv(uploaded_file)
            req_cols = ["日付", "市場番号", "カテゴリー", "商品コード", "商品名", "単価", "数量", "小計"]
            if all(col in df_up.columns for col in req_cols):
                st.dataframe(df_up.head())
                if st.button("📥 インポート実行", type="primary"):
                    insert_records_to_db(current_user, df_up.to_dict("records"))
                    st.session_state.records_df = load_records_from_db(current_user)
                    st.session_state.editor_version += 1
                    st.success("インポート完了しました！")
                    st.rerun()
        except Exception as e:
            st.error(f"エラー: {e}")

# ==========================================
# タブ 4: 納品書・請求書発行
# ==========================================
with tab_print:
    st.markdown('<div class="section-title">🖨️ 納品書・請求書自動生成</div>', unsafe_allow_html=True)
    if df_current is not None and not df_current.empty:
        p_col1, p_col2, p_col3 = st.columns(3)
        with p_col1:
            doc_type = st.selectbox("帳票種別", ["納 品 書", "請 求 書", "領 収 書"])
        with p_col2:
            client_name = st.text_input("宛名", value="〇〇青果 宛")
        with p_col3:
            selected_target_date = st.selectbox("対象取引日", df_current["日付"].unique().tolist())
            
        target_records = df_current[df_current["日付"] == selected_target_date].to_dict("records")
        target_market_no = target_records[0]["市場番号"] if target_records else "M-001"
        
        invoice_html = generate_invoice_html(
            doc_type, client_name, target_market_no, selected_target_date, tuple([tuple(d.items()) for d in target_records])
        )
        st.components.v1.html(invoice_html, height=500, scrolling=True)
    else:
        st.info("💡 発行対象データがありません。")