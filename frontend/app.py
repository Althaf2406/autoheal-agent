
import streamlit as st

# Konfigurasi Halaman
st.set_page_config(page_title="AutoHeal Agent Dashboard", layout="wide")

# Custom Premium CSS (NVIDIA Green Aesthetic)
st.markdown("""
<style>
    /* Gradient Text for Title */
    h1 {
        background: -webkit-linear-gradient(45deg, #76b900, #b2ff2e);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-weight: 800;
    }
    
    /* Sleek buttons with hover effects */
    .stButton > button {
        border-radius: 8px;
        transition: all 0.3s ease;
        border: 1px solid #76b900;
        background-color: transparent;
        color: #76b900;
    }
    .stButton > button:hover {
        background-color: #76b900;
        color: #000000;
        box-shadow: 0 4px 15px rgba(118, 185, 0, 0.4);
        transform: translateY(-2px);
    }
    
    /* Subheaders */
    h3 {
        color: #e0e0e0;
        border-bottom: 1px solid #3b5c00;
        padding-bottom: 5px;
        margin-bottom: 15px;
    }
    
    /* Fix text visibility in markdown and code blocks */
    code {
        color: #b2ff2e !important;
        background-color: #1a241c !important;
        -webkit-text-fill-color: initial !important; /* Fix if inside gradient h1 */
    }
    pre {
        background-color: #1a241c !important;
        border: 1px solid #3b5c00;
    }
    pre code {
        color: #e0e0e0 !important;
        background-color: transparent !important;
        -webkit-text-fill-color: initial !important;
    }
    
    /* Subtle container styling */
    .block-container {
        padding-top: 2rem !important;
    }
</style>
""", unsafe_allow_html=True)

# Judul Aplikasi
st.title("🛠️ AutoHeal Agent Dashboard")
st.markdown("Dashboard ini menampilkan proses perbaikan pipeline data secara otomatis (Multi-Agent).")

st.divider()

# Layout untuk Log Penerimaan Error (Sub-Task 1)
st.subheader("📡 Log Penerimaan Error (Webhook)")
st.caption("Pesan error dari GitHub Actions akan ditampilkan di bawah ini:")

# Placeholder kosong menggunakan st.empty()
log_container = st.empty()

# =====================================================================
# Bagian Bawah Dashboard (Anggota B): Kode Perbaikan LLM & Status Pull Request
# Data diambil dari backend lewat GET /runs
# =====================================================================
import difflib
import os

import requests

BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")

STATUS_LABELS = {
    "received": "📥 Diterima",
    "researching": "🕵️ Agent Researcher sedang menganalisis",
    "engineering": "🛠️ Agent Engineer sedang membuat perbaikan",
    "creating_pr": "🌿 Membuat Pull Request",
    "done": "✅ Selesai",
    "failed": "❌ Gagal",
}


def fetch_runs():
    try:
        resp = requests.get(f"{BACKEND_URL}/runs", timeout=5)
        resp.raise_for_status()
        return resp.json(), None
    except requests.RequestException as e:
        return [], str(e)


st.divider()
st.header("🛠️ Agent Engineer & Eksekusi")

def run_label(r):
    return f"{r['id']} · {r['received_at'][:19].replace('T', ' ')} · {STATUS_LABELS.get(r['status'], r['status'])}"


col_refresh, col_select = st.columns([1, 4])
with col_refresh:
    st.button("🔄 Refresh", width="stretch")

runs, fetch_error = fetch_runs()

if fetch_error:
    st.error(f"Tidak bisa terhubung ke backend ({BACKEND_URL}): {fetch_error}")
elif not runs:
    st.info("Belum ada run. Data akan muncul setelah webhook pertama diterima.")
else:
    runs_by_id = {r["id"]: r for r in runs}
    with col_select:
        selected_id = st.selectbox(
            "Pilih run",
            options=list(runs_by_id),
            format_func=lambda rid: run_label(runs_by_id[rid]),
        )
    run = runs_by_id[selected_id]
    payload = run.get("payload", {})
    st.caption(
        f"Repo: `{payload.get('repo', '-')}` · File: `{payload.get('file_path', '-')}` · "
        f"Status: **{STATUS_LABELS.get(run['status'], run['status'])}**"
    )

    # Mengisi placeholder bagian atas dengan error & analysis
    with log_container.container():
        st.subheader("🚨 Error Asli (Dari GitHub)")
        st.code(run.get("error_log", "Tidak ada error log"), language="python")
        
        analysis = run.get("analysis")
        if analysis:
            st.subheader("🕵️ Root Cause Analysis (Agent Researcher)")
            st.markdown(analysis)
        elif run["status"] == "received":
            st.info("Menunggu Agent Researcher...")

    # --- Kode Perbaikan LLM ---
    st.subheader("💻 Kode Perbaikan LLM")
    engineer = run.get("engineer")
    if not engineer:
        if run["status"] in ("received", "researching", "engineering"):
            st.info("Agent Engineer belum selesai. Tekan Refresh beberapa saat lagi.")
        else:
            st.warning("Tidak ada kode perbaikan untuk run ini.")
    else:
        if engineer.get("summary"):
            st.markdown(f"**Ringkasan perubahan:** {engineer['summary']}")
        if engineer.get("valid"):
            st.success(f"Kode lolos validasi ({engineer.get('language', '')}).")
        else:
            st.error(f"Kode tidak lolos validasi: {engineer.get('error')}")
        if engineer.get("fixed_code"):
            lang = "sql" if engineer.get("language") == "SQL" else "python"
            original = run.get("original_code")
            if original:
                tab_diff, tab_full = st.tabs(["Perubahan (diff)", "Kode lengkap"])
                with tab_diff:
                    file_name = payload.get("file_path", "file")
                    diff = "".join(difflib.unified_diff(
                        original.splitlines(keepends=True), engineer["fixed_code"].splitlines(keepends=True),
                        fromfile=f"a/{file_name}", tofile=f"b/{file_name}",
                    ))
                    st.code(diff or "Tidak ada perubahan.", language="diff")
                with tab_full:
                    st.code(engineer["fixed_code"], language=lang)
            else:
                st.code(engineer["fixed_code"], language=lang)

    # --- Status Keberhasilan Pull Request ---
    st.subheader("🌿 Status Pull Request")
    pr = run.get("pr")
    if pr and pr.get("pr_url"):
        if pr.get("reused"):
            st.info(f"Pull Request #{pr['pr_number']} dari run sebelumnya masih terbuka, jadi tidak dibuat PR baru.")
        else:
            st.success(f"Pull Request #{pr['pr_number']} berhasil dibuat.")
        st.markdown(f"🔗 [{pr['pr_url']}]({pr['pr_url']})")
        st.caption(f"Branch `{pr['branch']}` → `{pr['base_branch']}`")
        if st.button("Cek status PR di GitHub"):
            try:
                resp = requests.get(f"{BACKEND_URL}/runs/{run['id']}/pr-status", timeout=10)
                resp.raise_for_status()
                live = resp.json()
                st.info(f"Status PR saat ini: **{live.get('state', 'unknown')}**")
            except requests.RequestException as e:
                st.error(f"Gagal cek status PR: {e}")
    elif pr and pr.get("error"):
        st.warning(pr["error"])
    elif run["status"] == "failed":
        st.error(f"Pipeline gagal: {run.get('error')}")
    elif run["status"] == "creating_pr":
        st.info("Sedang membuat Pull Request...")
    else:
        st.info("Pull Request belum dibuat.")
