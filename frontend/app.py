
import streamlit as st

# Konfigurasi Halaman
st.set_page_config(page_title="AutoHeal Agent Dashboard", layout="wide")

# Judul Aplikasi
st.title("🛠️ AutoHeal Agent Dashboard")
st.markdown("Dashboard ini menampilkan proses perbaikan pipeline data secara otomatis (Multi-Agent).")

st.divider()

# Layout untuk Log Penerimaan Error (Sub-Task 1)
st.subheader("📡 Log Penerimaan Error (Webhook)")
st.caption("Pesan error dari GitHub Actions akan ditampilkan di bawah ini:")

# Placeholder kosong menggunakan st.empty() atau st.container() 
# Ini nantinya akan diisi secara dinamis dengan data dari backend
log_container = st.empty()

# Contoh isi sementara (placeholder)
with log_container.container():
    st.info("Menunggu pesan error dari GitHub Actions...")
    # Nanti kita bisa gunakan st.json() atau st.code() untuk menampilkan payload asli di sini.
