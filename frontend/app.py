
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

# Must match the statuses written by backend/graph.py (see run_store.create_run)
STATUS_LABELS = {
    "received": "📥 Diterima",
    "redacting": "🧹 Membersihkan data sensitif",
    "researching": "🕵️ Agent Researcher sedang menganalisis",
    "engineering": "🛠️ Agent Engineer sedang membuat perbaikan",
    "validating": "🛡️ Cek keamanan (SAST)",
    "testing": "🧪 Menjalankan test di sandbox",
    "creating_pr": "🌿 Membuat Pull Request",
    "escalating": "🆘 Mengeskalasi ke manusia",
    "resuming": "⏯️ Melanjutkan dari checkpoint",
    "done": "✅ Selesai",
    "duplicate": "🔁 Duplikat (komentar di PR yang ada)",
    "escalated": "🆘 Dieskalasi (GitHub Issue)",
    "failed": "❌ Gagal",
}
IN_PROGRESS = ("received", "redacting", "researching", "engineering", "validating", "testing", "resuming")
STAGE_LABELS = {"engineer": "Agent Engineer", "sast": "SAST / policy", "sandbox": "Sandbox test",
                "accepted": "Lolos"}


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
    if run.get("iteration"):
        st.caption(f"Iterasi Engineer: **{run['iteration']}** (maksimal 3)")
    if not engineer:
        if run["status"] in IN_PROGRESS:
            st.info("Agent Engineer belum selesai. Tekan Refresh beberapa saat lagi.")
        elif run["status"] == "duplicate":
            st.info("Error ini duplikat dari run sebelumnya, jadi Agent Engineer tidak dijalankan.")
        else:
            st.warning("Tidak ada kode perbaikan untuk run ini.")
    else:
        if engineer.get("summary"):
            st.markdown(f"**Ringkasan perubahan:** {engineer['summary']}")
        if not engineer.get("valid"):
            st.error(f"Agent Engineer tidak menghasilkan kode yang valid: {engineer.get('error')}")
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

    # --- Guardrails (SAST & destructive policy) ---
    sast = run.get("sast")
    if sast:
        st.subheader("🛡️ Guardrails (SAST)")
        if sast.get("passed"):
            st.success("Patch terakhir lolos semua lapisan pemeriksaan.")
        else:
            st.error("Patch terakhir ditolak guardrails." + (" Dieskalasi karena melanggar aturan keras."
                                                           if sast.get("escalate") else ""))
        rows = "\n".join(f"| {layer} | {result} |" for layer, result in sast.get("layers", {}).items())
        st.markdown(f"| Lapisan | Hasil |\n|---|---|\n{rows}")
        for reason in sast.get("reasons", []):
            st.markdown(f"- `{reason}`")

    # --- Sandbox test ---
    tests = run.get("tests")
    if tests:
        st.subheader("🧪 Sandbox Test")
        if tests.get("skipped"):
            st.warning(f"Test tidak dijalankan: {tests.get('output')}")
        else:
            reproduced = {True: "ya", False: "tidak (test sudah lolos sebelum patch)", None: "-"}[tests.get("reproduced")]
            st.caption(f"Mode `{tests.get('mode')}` · Perintah `{tests.get('command')}` · Error tereproduksi: {reproduced}")
            if tests.get("passed"):
                st.success("Test lolos setelah patch.")
            else:
                st.error("Test masih gagal setelah patch.")
            col_before, col_after = st.columns(2)
            with col_before:
                with st.expander("Sebelum patch"):
                    st.code(tests.get("before_output") or "-", language="text")
            with col_after:
                with st.expander("Sesudah patch"):
                    st.code(tests.get("output") or "-", language="text")

    # --- Riwayat iterasi (self-correction loop) ---
    attempts = run.get("attempts") or []
    if attempts:
        st.subheader("🔁 Riwayat Iterasi")
        for attempt in attempts:
            stage = STAGE_LABELS.get(attempt.get("stage"), attempt.get("stage"))
            icon = "✅" if attempt.get("stage") == "accepted" else "❌"
            with st.expander(f"{icon} Iterasi {attempt.get('iteration')}: {stage}"):
                if attempt.get("summary"):
                    st.markdown(f"**Ringkasan:** {attempt['summary']}")
                if attempt.get("feedback"):
                    st.markdown("**Alasan ditolak (dikirim balik ke Engineer):**")
                    st.code(attempt["feedback"], language="text")
                if attempt.get("diff"):
                    st.code(attempt["diff"], language="diff")

    # --- Status Keberhasilan Pull Request ---
    st.subheader("🌿 Status Pull Request")
    pr = run.get("pr")
    issue = run.get("issue")
    if pr and pr.get("pr_url"):
        if pr.get("reused"):
            st.info(f"Pull Request #{pr['pr_number']} dari run sebelumnya masih terbuka, jadi tidak dibuat PR baru.")
        else:
            kind = "Draft Pull Request" if pr.get("draft") else "Pull Request"
            st.success(f"{kind} #{pr['pr_number']} berhasil dibuat.")
        st.markdown(f"🔗 [{pr['pr_url']}]({pr['pr_url']})")
        labels = ", ".join(f"`{label}`" for label in pr.get("labels") or [])
        st.caption(f"Branch `{pr['branch']}` → `{pr['base_branch']}`" + (f" · Label {labels}" if labels else "")
                   + " · Wajib di-review manusia, bot tidak pernah merge.")
        if st.button("Cek status PR di GitHub"):
            try:
                resp = requests.get(f"{BACKEND_URL}/runs/{run['id']}/pr-status", timeout=10)
                resp.raise_for_status()
                live = resp.json()
                draft = " (draft)" if live.get("draft") else ""
                st.info(f"Status PR saat ini: **{live.get('state', 'unknown')}{draft}**")
            except requests.RequestException as e:
                st.error(f"Gagal cek status PR: {e}")
    elif run["status"] == "duplicate":
        st.info(f"Error yang sama terjadi lagi (x{run.get('occurrences') or '?'}). Tidak dibuat PR baru.")
        if pr and pr.get("comment_url"):
            st.markdown(f"💬 [Komentar di PR yang sudah ada]({pr['comment_url']})")
    elif run["status"] == "escalated":
        st.error(f"Agent menyerah dan mengeskalasi ke manusia: {run.get('error')}")
        if issue and issue.get("issue_url"):
            st.markdown(f"🆘 [GitHub Issue #{issue['issue_number']}]({issue['issue_url']})")
        elif issue and issue.get("error"):
            st.warning(issue["error"])
    elif pr and pr.get("error"):
        st.warning(pr["error"])
    elif run["status"] == "failed":
        st.error(f"Pipeline gagal: {run.get('error')}")
    elif run["status"] == "creating_pr":
        st.info("Sedang membuat Pull Request...")
    else:
        st.info("Pull Request belum dibuat.")
