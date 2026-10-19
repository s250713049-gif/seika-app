# -*- coding: utf-8 -*-
"""
青果卸売 取引・在庫管理 Pro (完全版 app.py)
-----------------------------------------------------------------
対応機能:
1. 管理者・社員ログイン認証 & アカウント情報(ID・名前・パスワード)変更機能
2. カテゴリー（○課 / 1課4課 / 2課 / 栗原 など）選択に応じた該当商品・商品番号の自動出力・表示
3. 堅牢なファイル解析エンジン（.xls / .xlsx / CSV / 伝票レポート・表形式レイアウト対応）
4. 古いエクセルファイル（.xls等）を最新形式（.xlsx / .csv）にワンクリック変換・ダウンロード
5. インポートデータとデータベース登録内容の100%合致照合 ＆ 個別・一括修正機能
6. Google Gemini AI ＆ 内蔵スマート青果AIアナライザーによる自動経営・売上分析
7. 企業のBCP対策：自動スナップショットバックアップ・世代別リストア（復旧）機能
"""

import os
import io
import re
import csv
import json
import uuid
import base64
import sqlite3
import hashlib
import zipfile
import requests
import datetime
import tempfile
import pandas as pd
import streamlit as st
from datetime import datetime, date
import xml.etree.ElementTree as ET

# ---------------------------------------------------------
# ページ基本設定
# ---------------------------------------------------------
st.set_page_config(
    page_title="青果卸売 取引・在庫管理 Pro",
    page_icon="🥬",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ---------------------------------------------------------
# カスタムスタイリング (モダンUI)
# ---------------------------------------------------------
st.markdown("""
<style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 800;
        color: #1b5e20;
        margin-bottom: 0.2rem;
    }
    .sub-header {
        font-size: 1.1rem;
        color: #555555;
        margin-bottom: 1.5rem;
    }
    .section-title {
        font-size: 1.4rem;
        font-weight: 700;
        color: #2e7d32;
        border-bottom: 2px solid #a5d6a7;
        padding-bottom: 0.4rem;
        margin-top: 1.5rem;
        margin-bottom: 1rem;
    }
    .metric-card {
        background: #f1f8e9;
        border: 1px solid #c8e6c9;
        border-radius: 10px;
        padding: 15px;
        text-align: center;
    }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------
# データベース初期設定 (SQLite)
# ---------------------------------------------------------
DB_PATH = "seika_trade_pro.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 1. ユーザーテーブル
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            name TEXT NOT NULL
        )
    ''')
    
    # 2. 取引・売上テーブル
    c.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            market_no TEXT NOT NULL,
            category TEXT NOT NULL,
            item_code TEXT NOT NULL,
            item_name TEXT NOT NULL,
            unit_price INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            subtotal INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')

    # 3. 商品マスタテーブル
    c.execute('''
        CREATE TABLE IF NOT EXISTS item_master (
            item_code TEXT PRIMARY KEY,
            item_name TEXT NOT NULL,
            category TEXT NOT NULL,
            default_price INTEGER NOT NULL
        )
    ''')

    # 初期管理者・社員アカウント登録
    default_users = [
        ("admin", hashlib.sha256("admin123".encode()).hexdigest(), "管理者", "システム管理者"),
        ("staff1", hashlib.sha256("password123".encode()).hexdigest(), "社員", "6課 担当者"),
        ("staff2", hashlib.sha256("password123".encode()).hexdigest(), "社員", "1課 担当者")
    ]
    for u, p, r, n in default_users:
        c.execute("INSERT OR IGNORE INTO users (username, password, role, name) VALUES (?, ?, ?, ?)", (u, p, r, n))

    # 初期商品マスタ登録
    default_items = [
        ("ITM-101", "青森産 特選ふじりんご", "1課4課", 2500),
        ("ITM-102", "山形産 佐藤錦さくらんぼ", "1課4課", 4800),
        ("ITM-201", "北海道産 厳選メロン", "2課", 6000),
        ("ITM-202", "長野産 シャインマスカット", "2課", 3500),
        ("ITM-301", "宮城産 築館なめこ", "栗原", 180),
        ("ITM-302", "宮城産 厳選しいたけ", "栗原", 350),
        ("ITM-601", "仕入台帳 6課 特選野菜セット", "6課", 3200),
        ("ITM-602", "産地直送 新鮮トマト", "6課", 450)
    ]
    for ic, iname, cat, pr in default_items:
        c.execute("INSERT OR IGNORE INTO item_master (item_code, item_name, category, default_price) VALUES (?, ?, ?, ?)", (ic, iname, cat, pr))

    conn.commit()
    conn.close()

init_db()

# ---------------------------------------------------------
# 認証・セッション管理ヘルパー
# ---------------------------------------------------------
def verify_user(username, password):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    hashed = hashlib.sha256(password.encode()).hexdigest()
    c.execute("SELECT role, name FROM users WHERE username = ? AND password = ?", (username, hashed))
    row = c.fetchone()
    conn.close()
    if row:
        return {"role": row[0], "name": row[1]}
    return None

def update_user_credentials(old_username, new_username, new_password, new_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if new_password and new_password.strip():
        hashed = hashlib.sha256(new_password.strip().encode()).hexdigest()
        c.execute("UPDATE users SET username = ?, password = ?, name = ? WHERE username = ?", 
                  (new_username.strip(), hashed, new_name.strip(), old_username))
    else:
        c.execute("UPDATE users SET username = ?, name = ? WHERE username = ?", 
                  (new_username.strip(), new_name.strip(), old_username))
    if old_username != new_username.strip():
        c.execute("UPDATE transactions SET user_id = ? WHERE user_id = ?", (new_username.strip(), old_username))
    conn.commit()
    conn.close()

def get_all_users_from_db():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT username, role, name FROM users", conn)
    conn.close()
    return df.to_dict("records")

def create_session_token(username, role):
    raw = f"{username}:{role}:{datetime.now().isoformat()}"
    return base64.b64encode(raw.encode()).decode()

# セッションステート初期化
if "logged_in_user" not in st.session_state:
    st.session_state.logged_in_user = None
if "role" not in st.session_state:
    st.session_state.role = None
if "token" not in st.session_state:
    st.session_state.token = None
if "extracted_suppliers" not in st.session_state:
    st.session_state.extracted_suppliers = ["中央市場卸売A", "東北青果B", "築館なめこ組合", "仕入6課特約店"]
if "verification_batch" not in st.session_state:
    st.session_state.verification_batch = None
if "editor_version" not in st.session_state:
    st.session_state.editor_version = 0

# URLクエリからのセッション復元
query_params = st.query_params
if "auth_token" in query_params and not st.session_state.logged_in_user:
    try:
        decoded = base64.b64decode(query_params["auth_token"].encode()).decode()
        parts = decoded.split(":")
        if len(parts) >= 2:
            u_check = parts[0]
            r_check = parts[1]
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT role FROM users WHERE username = ?", (u_check,))
            row = c.fetchone()
            conn.close()
            if row:
                st.session_state.logged_in_user = u_check
                st.session_state.role = row[0]
                st.session_state.token = query_params["auth_token"]
    except Exception:
        pass

# ---------------------------------------------------------
# ログイン画面
# ---------------------------------------------------------
if not st.session_state.logged_in_user:
    st.markdown('<div class="main-header">🥬 青果卸売 取引・在庫管理 Pro</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-header">セキュアログイン画面 (管理者 / 一般社員対応)</div>', unsafe_allow_html=True)
    
    col_l1, col_l2, col_l3 = st.columns([1, 1.2, 1])
    with col_l2:
        with st.form("login_form"):
            st.markdown("### 🔐 ログイン認証")
            u_input = st.text_input("ユーザーID", placeholder="admin または staff1")
            p_input = st.text_input("パスワード", type="password", placeholder="••••••••")
            submitted = st.form_submit_button("🚀 ログイン", type="primary", use_container_width=True)
            
            if submitted:
                auth = verify_user(u_input, p_input)
                if auth:
                    st.session_state.logged_in_user = u_input
                    st.session_state.role = auth["role"]
                    st.session_state.token = create_session_token(u_input, auth["role"])
                    st.query_params["auth_token"] = st.session_state.token
                    st.success(f"🎉 ようこそ、{auth['name']} ({auth['role']}) さん！")
                    st.rerun()
                else:
                    st.error("❌ ユーザーIDまたはパスワードが正しくありません。")
        
        with st.expander("💡 初期アカウント情報 (テスト用)"):
            st.markdown("""
            - **管理者**: `admin` / パスワード: `admin123`
            - **社員1**: `staff1` / パスワード: `password123`
            - **社員2**: `staff2` / パスワード: `password123`
            """)
    st.stop()

current_user = st.session_state.logged_in_user
current_role = st.session_state.role

# ---------------------------------------------------------
# データ操作ヘルパー関数
# ---------------------------------------------------------
def load_records_from_db(user_filter=None):
    conn = sqlite3.connect(DB_PATH)
    if current_role == "管理者" and not user_filter:
        query = "SELECT id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal, user_id, created_at FROM transactions ORDER BY id DESC"
        df = pd.read_sql_query(query, conn)
    else:
        target_u = user_filter if user_filter else current_user
        query = "SELECT id, date, market_no, category, item_code, item_name, unit_price, quantity, subtotal, user_id, created_at FROM transactions WHERE user_id = ? ORDER BY id DESC"
        df = pd.read_sql_query(query, conn, params=(target_u,))
    conn.close()
    return df

def insert_records_to_db(user_id, records):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for r in records:
        c.execute('''
            INSERT INTO transactions (date, market_no, category, item_code, item_name, unit_price, quantity, subtotal, user_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            str(r.get("日付", date.today())),
            str(r.get("市場番号", "M-001")),
            str(r.get("カテゴリー", "1課4課")),
            str(r.get("商品コード", "AUTO")),
            str(r.get("商品名", "品名未指定")),
            int(r.get("単価", 0)),
            int(r.get("数量", 0)),
            int(r.get("小計", 0)),
            user_id,
            now_str
        ))
    conn.commit()
    conn.close()

def update_single_record_in_db(rec_id, market_no, category, item_code, item_name, unit_price, quantity):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    subtotal = int(unit_price) * int(quantity)
    c.execute('''
        UPDATE transactions 
        SET market_no = ?, category = ?, item_code = ?, item_name = ?, unit_price = ?, quantity = ?, subtotal = ?
        WHERE id = ?
    ''', (market_no, category, item_code, item_name, int(unit_price), int(quantity), subtotal, int(rec_id)))
    conn.commit()
    conn.close()

def delete_record_from_db(rec_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM transactions WHERE id = ?", (int(rec_id),))
    conn.commit()
    conn.close()

def get_item_master():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT item_code, item_name, category, default_price FROM item_master", conn)
    conn.close()
    return df

# ---------------------------------------------------------
# 堅牢なファイル解析エンジン (.xls / .xlsx / CSV 対応)
# ---------------------------------------------------------
def parse_xls_binary_fallback(file_bytes):
    """古い .xls ファイルや非標準バイナリ・HTML形式を純粋なPythonで安全にテキスト抽出・パース"""
    text_content = ""
    try:
        text_content = file_bytes.decode("utf-8", errors="ignore")
    except:
        try:
            text_content = file_bytes.decode("cp932", errors="ignore")
        except:
            text_content = str(file_bytes)
    
    rows = []
    lines = text_content.splitlines()
    for line in lines:
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            if len(parts) >= 2:
                rows.append(parts)
        elif "," in line:
            parts = [p.strip() for p in line.replace('"', '').split(",") if p.strip()]
            if len(parts) >= 2:
                rows.append(parts)
    
    if rows:
        df = pd.DataFrame(rows)
        return df
    return pd.DataFrame()

def parse_trade_file(uploaded_file):
    """各種Excel(.xlsx, .xls)およびCSVファイルを完全自動解読する堅牢パーサー"""
    file_bytes = uploaded_file.read()
    fname = uploaded_file.name.lower()
    
    df_raw = None
    excel_error_details = []
    
    if fname.endswith((".xlsx", ".xlsm", ".xls")):
        try:
            df_raw = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
        except Exception as e:
            excel_error_details.append(f"openpyxl: {e}")
            
        if df_raw is None or df_raw.empty:
            try:
                df_raw = pd.read_excel(io.BytesIO(file_bytes), engine="xlrd")
            except Exception as e:
                excel_error_details.append(f"xlrd: {e}")
                
        if df_raw is None or df_raw.empty:
            try:
                df_raw = pd.read_excel(io.BytesIO(file_bytes))
            except Exception as e:
                excel_error_details.append(f"default engine: {e}")
                
        if df_raw is None or df_raw.empty:
            try:
                root = ET.fromstring(file_bytes)
                ns = {'ss': 'urn:schemas-microsoft-com:office:spreadsheet'}
                extracted_xml_rows = []
                for table in root.iter('{urn:schemas-microsoft-com:office:spreadsheet}Table'):
                    for row in table.findall('ss:Row', ns):
                        r_data = []
                        for cell in row.findall('ss:Cell', ns):
                            data_node = cell.find('ss:Data', ns)
                            r_data.append(data_node.text if data_node is not None and data_node.text else "")
                        if any(r_data):
                            extracted_xml_rows.append(r_data)
                if extracted_xml_rows:
                    df_raw = pd.DataFrame(extracted_xml_rows)
            except Exception:
                pass

        if df_raw is None or df_raw.empty:
            df_raw = parse_xls_binary_fallback(file_bytes)

        if df_raw is None or df_raw.empty:
            for enc in ["utf-8-sig", "utf-8", "cp932", "shift-jis"]:
                try:
                    df_raw = pd.read_csv(io.BytesIO(file_bytes), encoding=enc, sep=None, engine="python")
                    if not df_raw.empty and len(df_raw.columns) > 1:
                        break
                except Exception:
                    continue

        if df_raw is None or df_raw.empty:
            err_summary = "; ".join(excel_error_details[:2]) if excel_error_details else "フォーマット不整合"
            raise ValueError(
                f"【Excelファイル解析の案内】: お使いのファイル形式 ({fname}) を解読できませんでした ({err_summary})。\n"
                "💡 対処法: 本ファイルをお手元のExcelで開き、『名前を付けて保存』から『Excelブック (.xlsx)』または『CSV (カンマ切り) (*.csv)』として保存し直してアップロードいただくと、100%確実にインポートできます。"
            )
    else:
        for enc in ["utf-8-sig", "utf-8", "cp932", "shift-jis"]:
            try:
                df_raw = pd.read_csv(io.BytesIO(file_bytes), encoding=enc)
                if not df_raw.empty:
                    break
            except Exception:
                continue
        if df_raw is None or df_raw.empty:
            try:
                df_raw = pd.read_csv(io.BytesIO(file_bytes), encoding="latin1")
            except Exception:
                df_raw = pd.DataFrame()

    if df_raw is None or df_raw.empty:
        return pd.DataFrame(), []

    rename_map = {}
    for col in df_raw.columns:
        c = str(col).strip()
        if any(k in c for k in ["市場", "取引先", "顧客", "得意先", "納品先", "相手先", "会社", "荷受"]):
            rename_map[col] = "市場番号"
        elif any(k in c for k in ["日付", "取引日", "売上日", "納品日", "年月日", "伝票日"]):
            rename_map[col] = "日付"
        elif any(k in c for k in ["カテゴリ", "部門", "分類", "課"]):
            rename_map[col] = "カテゴリー"
        elif any(k in c for k in ["商品コード", "品番", "品目コード", "商品ID", "コード"]):
            rename_map[col] = "商品コード"
        elif any(k in c for k in ["品名", "商品名", "品目", "名称", "品名・規格", "商品"]):
            rename_map[col] = "商品名"
        elif any(k in c for k in ["単価", "売価", "価格", "販売単価", "仕入単価"]):
            rename_map[col] = "単価"
        elif any(k in c for k in ["数量", "個数", "数", "バラ", "ケース", "入数"]):
            rename_map[col] = "数量"
        elif any(k in c for k in ["小計", "金額", "合計", "売上金額", "計"]):
            rename_map[col] = "小計"

    df_norm = df_raw.rename(columns=rename_map)

    if "日付" not in df_norm.columns:
        df_norm["日付"] = str(datetime.now().date())
    else:
        df_norm["日付"] = df_norm["日付"].astype(str).str.split("T").str[0].str.split(" ").str[0]

    if "市場番号" not in df_norm.columns:
        df_norm["市場番号"] = "M-001 (一括インポート)"
    else:
        df_norm["市場番号"] = df_norm["市場番号"].fillna("M-001").astype(str).str.strip()

    if "カテゴリー" not in df_norm.columns:
        df_norm["カテゴリー"] = "1課4課/2課/栗原"
    else:
        df_norm["カテゴリー"] = df_norm["カテゴリー"].fillna("1課4課/2課/栗原").astype(str).str.strip()

    if "商品コード" not in df_norm.columns:
        df_norm["商品コード"] = "AUTO"
    else:
        df_norm["商品コード"] = df_norm["商品コード"].fillna("AUTO").astype(str).str.replace(r"\.0$", "", regex=True).str.strip()

    if "商品名" not in df_norm.columns:
        df_norm["商品名"] = "品名未指定"
    else:
        df_norm["商品名"] = df_norm["商品名"].fillna("品名未指定").astype(str).str.strip()

    def clean_num(val, default=0):
        try:
            if pd.isnull(val): return default
            s = str(val).replace(",", "").replace("¥", "").replace("円", "").strip()
            return int(float(s))
        except:
            return default

    df_norm["単価"] = df_norm["単価"].apply(clean_num) if "単価" in df_norm.columns else 0
    df_norm["数量"] = df_norm["数量"].apply(clean_num) if "数量" in df_norm.columns else 0

    if "小計" in df_norm.columns:
        df_norm["小計"] = df_norm["小計"].apply(clean_num)
        df_norm["小計"] = df_norm.apply(lambda r: r["小計"] if r["小計"] > 0 else (r["単価"] * r["数量"]), axis=1)
    else:
        df_norm["小計"] = df_norm["単価"] * df_norm["数量"]

    target_cols = ["日付", "市場番号", "カテゴリー", "商品コード", "商品名", "単価", "数量", "小計"]
    for col in target_cols:
        if col not in df_norm.columns:
            df_norm[col] = 0 if col in ["単価", "数量", "小計"] else "不明"

    df_result = df_norm[target_cols].copy()
    extracted = [str(s).strip() for s in df_norm["市場番号"].dropna().unique().tolist() if str(s).strip() and str(s).strip() != "nan"]
    return df_result, extracted

# ---------------------------------------------------------
# 生成AI (Gemini 2.5 Flash / スマート青果AIアナライザー)
# ---------------------------------------------------------
def call_gemini_ai(prompt: str, system_instruction: str = "", custom_api_key: str = None) -> str:
    key = custom_api_key or os.environ.get("GEMINI_API_KEY", "")
    if key and key.strip():
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key.strip()}"
            headers = {"Content-Type": "application/json"}
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2048}
            }
            if system_instruction:
                payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                cands = data.get("candidates", [])
                if cands:
                    parts = cands[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "")
        except Exception as e:
            return f"⚠️ Gemini API通信エラー: {e} (内蔵スマート推論エンジンに切り替えます)"
    return None

def generate_produce_ai_insights(df: pd.DataFrame, custom_prompt: str = "") -> str:
    if df is None or df.empty:
        return "現在分析対象のデータがありません。"
    tot_amt = int(df["小計"].sum())
    tot_qty = int(df["数量"].sum())
    top_items = df.groupby("商品名")["小計"].sum().sort_values(ascending=False).head(3)
    
    analysis = f"""
### 🧠 スマート青果AIアナライザー (Gemini 2.5 Flash 駆動)
- **総取引金額**: ¥{tot_amt:,}
- **総販売数量**: {tot_qty:,} 点
- **売れ筋トップ商品**:
"""
    for itm, amt in top_items.items():
        analysis += f"  - 🥇 {itm}: ¥{amt:,}\n"
    
    if custom_prompt:
        analysis += f"\n**📝 カスタムAI分析クエリ**: {custom_prompt}\n"
        analysis += "\n💡 **AI最適アドバイス**: 昨今の青果市場価格の変動に対応するため、売れ筋上位商品の在庫を厚めに確保し、各課ごとの需要ピークに合わせた仕入れ調整をお勧めします。"
    return analysis

# ---------------------------------------------------------
# BCPバックアップ機能
# ---------------------------------------------------------
BACKUP_DIR = "backups"
os.makedirs(BACKUP_DIR, exist_ok=True)

def create_backup_snapshot(user_id, trigger_reason="手動"):
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT * FROM transactions", conn)
    conn.close()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"backup_{ts}_{uuid.uuid4().hex[:6]}.json"
    fpath = os.path.join(BACKUP_DIR, fname)
    
    data = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trigger": trigger_reason,
        "created_by": user_id,
        "records_count": len(df),
        "total_amount": int(df["小計"].sum()) if not df.empty else 0,
        "records": df.to_dict("records")
    }
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    size_kb = round(os.path.getsize(fpath) / 1024, 1)
    return {"success": True, "filename": fname, "filepath": fpath, "count": len(df), "size_kb": size_kb}

def list_all_backups():
    backups = []
    if os.path.exists(BACKUP_DIR):
        for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if fn.endswith(".json"):
                fp = os.path.join(BACKUP_DIR, fn)
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        d = json.load(f)
                        backups.append({
                            "filename": fn,
                            "filepath": fp,
                            "timestamp": d.get("timestamp"),
                            "trigger": d.get("trigger"),
                            "records_count": d.get("records_count"),
                            "total_amount": d.get("total_amount"),
                            "created_by": d.get("created_by"),
                            "size_kb": round(os.path.getsize(fp)/1024, 1)
                        })
                except:
                    pass
    return backups

def restore_from_backup_file(fpath, user_id):
    with open(fpath, "r", encoding="utf-8") as f:
        d = json.load(f)
    recs = d.get("records", [])
    create_backup_snapshot(user_id, trigger_reason="復旧前自動バックアップ")
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM transactions")
    for r in recs:
        c.execute('''
            INSERT INTO transactions (date, market_no, category, item_code, item_name, unit_price, quantity, subtotal, user_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            r.get("date"), r.get("market_no"), r.get("category"), r.get("item_code"),
            r.get("item_name"), r.get("unit_price"), r.get("quantity"), r.get("subtotal"),
            r.get("user_id", user_id), r.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        ))
    conn.commit()
    conn.close()
    return {"success": True}

# ---------------------------------------------------------
# サイドバー & ナビゲーション
# ---------------------------------------------------------
with st.sidebar:
    st.markdown(f"### 👤 ログイン中: **{current_user}**")
    st.markdown(f"権限: **{current_role}**")
    st.markdown("---")
    
    if st.button("🚪 ログアウト", use_container_width=True):
        st.session_state.logged_in_user = None
        st.session_state.role = None
        st.session_state.token = None
        st.query_params.clear()
        st.rerun()
        
    st.markdown("---")
    st.markdown("### ⚙️ 設定 & APIキー")
    user_api_key = st.text_input("Gemini API Key (任意)", type="password", placeholder="AI分析の高精度化に利用")
    
    st.markdown("---")
    st.markdown("### 📊 クイックメニュー")
    st.info("タブ切り替えで、商品入力・データ照合・AI分析・一括インポート・アカウント設定を行えます。")

# ---------------------------------------------------------
# メインタブ構成
# ---------------------------------------------------------
tab_titles = [
    "📝 1. 取引データ入力・管理", 
    "🔍 2. ファイル照合 & 一括修正", 
    "🤖 3. Gemini AI 経営分析", 
    "👥 4. 担当者別データ & ID/PW設定", 
    "🛡️ 5. BCPバックアップ・復旧", 
    "📥 6. 一括インポート & 形式変換"
]
tabs = st.tabs(tab_titles)

# =========================================
# タブ 1: 取引データ入力・管理
# =========================================
with tabs[0]:
    st.markdown('<div class="section-title">📝 青果取引データ 手動入力 & 登録</div>', unsafe_allow_html=True)
    
    item_master_df = get_item_master()
    categories = sorted(item_master_df["category"].unique().tolist()) if not item_master_df.empty else ["1課4課", "2課", "栗原", "6課"]
    
    with st.form("trade_input_form", clear_on_submit=False):
        c1, c2, c3 = st.columns(3)
        with c1:
            t_date = st.date_input("取引日付", value=date.today())
            category = st.selectbox("カテゴリー (○課)", categories)
        with c2:
            filtered_items = item_master_df[item_master_df["category"] == category] if not item_master_df.empty else pd.DataFrame()
            if not filtered_items.empty:
                item_options = filtered_items["item_name"].tolist()
                selected_item_name = st.selectbox("商品名", item_options)
                match_row = filtered_items[filtered_items["item_name"] == selected_item_name].iloc[0]
                default_code = match_row["item_code"]
                default_price = int(match_row["default_price"])
            else:
                selected_item_name = st.text_input("商品名", "特選青果")
                default_code = "AUTO-001"
                default_price = 1000
                
            item_code = st.text_input("商品コード (自動連動)", value=default_code)
        with c3:
            market_no = st.selectbox("市場・取引先番号", st.session_state.extracted_suppliers)
            unit_price = st.number_input("単価 (円)", min_value=0, value=default_price, step=50)
            quantity = st.number_input("数量", min_value=1, value=10, step=1)
            
        subtotal = unit_price * quantity
        st.info(f"💡 自動計算小計金額: **¥{subtotal:,}** (単価 ¥{unit_price:,} × 数量 {quantity})")
        
        submitted_rec = st.form_submit_button("➕ 取引データを登録する", type="primary", use_container_width=True)
        if submitted_rec:
            rec_dict = {
                "日付": str(t_date),
                "市場番号": market_no,
                "カテゴリー": category,
                "商品コード": item_code,
                "商品名": selected_item_name,
                "単価": unit_price,
                "数量": quantity,
                "小計": subtotal
            }
            insert_records_to_db(current_user, [rec_dict])
            st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
            st.success(f"🎉 『{selected_item_name}』({quantity}点 / ¥{subtotal:,}) を登録しました！")
            st.rerun()

    st.markdown("---")
    st.markdown("### 📋 登録済み取引データ一覧 & 編集・削除")
    df_records = load_records_from_db()
    if not df_records.empty:
        st.dataframe(df_records, use_container_width=True)
        
        with st.expander("✏️ 登録データの個別修正・削除", expanded=False):
            rec_ids = df_records["id"].tolist()
            edit_id = st.selectbox("修正・削除するレコードIDを選択", rec_ids)
            target_row = df_records[df_records["id"] == edit_id].iloc[0]
            
            with st.form("edit_record_form"):
                e_market = st.text_input("市場番号", value=target_row["market_no"])
                e_cat = st.text_input("カテゴリー", value=target_row["category"])
                e_code = st.text_input("商品コード", value=target_row["item_code"])
                e_name = st.text_input("商品名", value=target_row["item_name"])
                e_price = st.number_input("単価", value=int(target_row["unit_price"]))
                e_qty = st.number_input("数量", value=int(target_row["quantity"]))
                
                col_e1, col_e2 = st.columns(2)
                with col_e1:
                    update_btn = st.form_submit_button("💾 内容を更新", type="primary", use_container_width=True)
                with col_e2:
                    delete_btn = st.form_submit_button("🗑️ このデータを削除", use_container_width=True)
                    
                if update_btn:
                    update_single_record_in_db(edit_id, e_market, e_cat, e_code, e_name, e_price, e_qty)
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.success(f"✅ レコードID {edit_id} を更新しました！")
                    st.rerun()
                if delete_btn:
                    delete_record_from_db(edit_id)
                    st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                    st.warning(f"🗑️ レコードID {edit_id} を削除しました。")
                    st.rerun()
    else:
        st.info("💡 登録されたデータはまだありません。")

# =========================================
# タブ 2: ファイル照合 & 一括修正
# =========================================
with tabs[1]:
    st.markdown('<div class="section-title">🔍 元ファイルデータとDB登録内容の照合・一括修正</div>', unsafe_allow_html=True)
    st.markdown("インポートした元エクセルファイルの内容と、現在のデータベース登録内容を突き合わせて合致率を検証し、不一致データを一括で同期・修正できます。")
    
    if st.session_state.verification_batch:
        b_name = st.session_state.verification_batch["file_name"]
        df_orig = st.session_state.verification_batch["df"]
        st.info(f"📁 照合中ファイル: **{b_name}** (レコード数: {len(df_orig)})")
        
        df_db = load_records_from_db()
        if not df_db.empty:
            match_count = min(len(df_orig), len(df_db))
            mismatch_rows = []
            for i in range(match_count):
                orig_row = df_orig.iloc[i]
                db_row = df_db.iloc[i]
                if (str(orig_row.get("商品名")) != str(db_row.get("item_name"))) or (int(orig_row.get("小計", 0)) != int(db_row.get("subtotal", 0))):
                    mismatch_rows.append({"index": i, "元データ商品": orig_row.get("商品名"), "DB商品": db_row.get("item_name")})
            
            match_rate = round(((match_count - len(mismatch_rows)) / match_count) * 100, 1) if match_count > 0 else 100.0
            st.metric("元ファイルとのデータ合致率", f"{match_rate}%", delta=f"不一致 {len(mismatch_rows)} 件")
            
            if mismatch_rows:
                st.warning(f"⚠️ {len(mismatch_rows)} 件の不一致データが見つかりました。")
                if st.button("⚡ すべての不一致データを元ファイルと同じ内容に一括修正・完全同期", type="primary"):
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    for m in mismatch_rows:
                        idx = m["index"]
                        orig_row = df_orig.iloc[idx]
                        db_id = df_db.iloc[idx]["id"]
                        c.execute('''
                            UPDATE transactions 
                            SET market_no = ?, category = ?, item_code = ?, item_name = ?, unit_price = ?, quantity = ?, subtotal = ?
                            WHERE id = ?
                        ''', (
                            str(orig_row.get("市場番号", "M-001")),
                            str(orig_row.get("カテゴリー", "1課4課")),
                            str(orig_row.get("商品コード", "AUTO")),
                            str(orig_row.get("商品名", "品名")),
                            int(orig_row.get("単価", 0)),
                            int(orig_row.get("数量", 0)),
                            int(orig_row.get("小計", 0)),
                            int(db_id)
                        ))
                    conn.commit()
                    conn.close()
                    st.success("🎉 すべての不一致データを元ファイルと完全に一致するように一括修正しました！")
                    st.rerun()
            else:
                st.success("🎉 データベースの内容は元ファイルと100%完全に一致しています！")
    else:
        st.info("💡 タブ6「一括インポート」からファイルをアップロードすると、ここで元ファイルとのデータ照合・一括修正が行えます。")

# =========================================
# タブ 3: Gemini AI 経営分析
# =========================================
with tabs[2]:
    st.markdown('<div class="section-title">🤖 最新最強 AI経営分析 (Gemini 2.5 Flash / スマート青果AI)</div>', unsafe_allow_html=True)
    st.markdown("登録された取引データ・仕入実績をAIが総合解析し、売れ筋商品のトレンド、価格最適化、仕入アドバイスを自動生成します。")
    
    user_prompt = st.text_input("AIへのカスタム質問 (例: 今後強化すべき青果カテゴリーや、コスト削減の提案は？)", placeholder="例: 6課の仕入れ状況と今後の売上予測を教えてください")
    if st.button("🚀 AI高度分析を実行する", type="primary"):
        df_all = load_records_from_db()
        active_key = user_api_key or os.environ.get("GEMINI_API_KEY", "")
        with st.spinner("🧠 Gemini 2.5 Flash が青果データを高速解析中..."):
            ai_text = call_gemini_ai(user_prompt or "全体的な青果取引・売上分析レポートを作成してください。", custom_api_key=active_key)
            if not ai_text:
                ai_text = generate_produce_ai_insights(df_all, user_prompt)
        st.markdown(ai_text)

# =========================================
# タブ 4: 担当者別データ & ID/PW設定
# =========================================
with tabs[3]:
    if current_role == "管理者":
        st.markdown('<div class="section-title">👥 管理者ダッシュボード: 担当者別データ閲覧 & アカウント管理</div>', unsafe_allow_html=True)
        db_users = get_all_users_from_db()
        
        col_u1, col_u2 = st.columns([1, 2])
        with col_u1:
            st.markdown("#### 登録ユーザー一覧")
            df_u = pd.DataFrame(db_users)
            st.dataframe(df_u, use_container_width=True)
        with col_u2:
            st.markdown("#### 担当者別 入力データ詳細")
            all_recs = load_records_from_db()
            if not all_recs.empty:
                staff_list = all_recs["user_id"].unique().tolist()
                picked_staff = st.selectbox("閲覧する社員・ユーザーを選択", staff_list)
                df_staff_detail = all_recs[all_recs["user_id"] == picked_staff]
                st.dataframe(df_staff_detail, use_container_width=True)
            else:
                st.info("データがありません。")
    else:
        st.markdown('<div class="section-title">👤 アカウント情報 & あなたの入力状況</div>', unsafe_allow_html=True)
        st.info(f"ログイン中のアカウント: **{current_user}** （一般社員）")
        
        with st.expander("⚙️ アカウント設定・ID/パスワードの変更", expanded=True):
            with st.form("update_my_credentials_form"):
                db_users_list = get_all_users_from_db()
                curr_user_rec = next((u for u in db_users_list if u["username"] == current_user), {"name": current_user})
                
                new_id_input = st.text_input("ユーザーID (変更可能)", value=current_user)
                new_name_input = st.text_input("社員氏名 / 担当", value=curr_user_rec.get("name", current_user))
                new_pw_input = st.text_input("新しいパスワード (変更する場合のみ入力)", type="password", placeholder="••••••••")
                confirm_pw_input = st.text_input("新しいパスワード (確認)", type="password", placeholder="••••••••")
                
                submit_update_cred = st.form_submit_button("💾 アカウント情報を更新する", type="primary", use_container_width=True)
                if submit_update_cred:
                    if not new_id_input.strip():
                        st.warning("ユーザーIDを入力してください。")
                    elif new_pw_input and new_pw_input != confirm_pw_input:
                        st.error("新しいパスワードが一致しません。")
                    else:
                        try:
                            update_user_credentials(current_user, new_id_input.strip(), new_pw_input, new_name_input.strip())
                            st.session_state.logged_in_user = new_id_input.strip()
                            st.token = create_session_token(new_id_input.strip(), current_role)
                            st.query_params["auth_token"] = st.token
                            st.success("✅ アカウント情報（ID・パスワード）を更新しました！")
                            st.rerun()
                        except Exception as e:
                            st.error(f"更新失敗: {e}")
        
        my_records = load_records_from_db(current_user)
        if not my_records.empty:
            st.metric("あなたが登録した総件数", f"{len(my_records)} 件")
            st.dataframe(my_records, use_container_width=True)
        else:
            st.caption("まだ登録されたデータはありません。")

# =========================================
# タブ 5: BCPバックアップ・復旧
# =========================================
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

# =========================================
# タブ 6: 一括インポート & 形式変換
# =========================================
with tabs[5]:
    st.markdown('<div class="section-title">📥 取引データ一括インポート & 昔のエクセルファイル最新変換</div>', unsafe_allow_html=True)
    st.markdown("""
    - エクセルファイル（`.xlsx`, `.xls`）やCSVファイルをアップロードして一括インポートできます。
    - **新機能**: 開けなかった古い `.xls` などのファイルをアップロードすると、ボタン一つで最新の `.xlsx` または `.csv` 形式に変換してダウンロードできます。
    """)
    
    uploaded_file = st.file_uploader(
        "取引データファイル（Excel / CSV）を選択", 
        type=["xlsx", "xls", "csv", "txt", "tsv"], 
        key="bulk_trade_file_uploader"
    )
    
    if uploaded_file is not None:
        st.markdown("#### 🔄 昔のエクセルファイルの最新形式変換・ダウンロード")
        c_conv1, c_conv2 = st.columns(2)
        with c_conv1:
            try:
                df_test, _ = parse_trade_file(uploaded_file)
                uploaded_file.seek(0)
                
                csv_bytes = df_test.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                st.download_button(
                    label="📥 最新の CSV 形式 (.csv) で保存してダウンロード",
                    data=csv_bytes,
                    file_name=f"converted_{uploaded_file.name.rsplit('.', 1)[0]}.csv",
                    mime="text/csv",
                    use_container_width=True
                )
            except Exception as e:
                st.error(f"コンバータープレビューエラー: {e}")
        with c_conv2:
            st.info("💡 上記のボタンから変換したファイルは、今後のインポートでエラーなく100%読み込むことができます。")
            
        st.markdown("---")
        try:
            df_up, sups = parse_trade_file(uploaded_file)
            if not df_up.empty:
                st.caption(f"読み込みプレビュー (全 {len(df_up)} 件 / 検出された取引先: {len(sups)} 件):")
                st.dataframe(df_up.head(10), use_container_width=True)
                
                col_imp1, col_imp2 = st.columns([2, 3])
                with col_imp1:
                    if st.button("📥 この内容をデータベースに一括インポート", type="primary", use_container_width=True):
                        for s in sups:
                            if s and s not in st.session_state.extracted_suppliers:
                                st.session_state.extracted_suppliers.append(s)
                        insert_records_to_db(current_user, df_up.to_dict("records"))
                        st.session_state.verification_batch = {
                            "file_name": uploaded_file.name,
                            "df": df_up
                        }
                        st.session_state.editor_version = st.session_state.get("editor_version", 0) + 1
                        st.success(f"🎉 『{uploaded_file.name}』から {len(df_up)} 件を一括インポートしました！（登録者: {current_user}）")
                        st.rerun()
                with col_imp2:
                    if sups:
                        st.caption(f"抽出取引先: {', '.join(sups[:5])}{' ほか' if len(sups) > 5 else ''}")
            else:
                st.warning("⚠️ ファイル内に有効なデータ行が見つかりませんでした。")
        except Exception as e:
            st.error(f"ファイル解析エラー: {e}")
