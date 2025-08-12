# Order Splitter — Single File (dual PDF backends: PyPDF2 or pypdf)
import re, io, os, csv, sys
import streamlit as st

st.set_page_config(page_title="Order Splitter — Single File (Dual PDF Backends)", layout="wide")
st.title("Order Splitter")
st.caption("Single-file app • uses **PyPDF2** if available, else **pypdf** • per-vendor PDFs • CSV log")

# ---------- Diagnostics ----------
with st.expander("Environment diagnostics"):
    st.write({"python": sys.version})
    try:
        from importlib.metadata import version, PackageNotFoundError
        def v(pkg):
            try: return version(pkg)
            except PackageNotFoundError: return "not installed"
        st.write({
            "PyPDF2": v("PyPDF2"),
            "pypdf": v("pypdf"),
            "pandas": v("pandas"),
            "openpyxl": v("openpyxl"),
            "streamlit": v("streamlit"),
        })
    except Exception as e:
        st.write({"importlib.metadata": f"error: {e}"})

# ---------- Settings ----------
ZERO_SIGNIFICANT = st.sidebar.checkbox("Treat leading zeros as significant", value=False)

ANCHOR_PATTERNS = [
    (r"(Model(?:\s*#|(?:\s*Number)?)\s*[:\-]?\s*)([A-Z0-9][A-Z0-9\-/_]{2,})", "Model"),
    (r"(Item\s*#\s*[:\-]?\s*)([A-Z0-9][A-Z0-9\-/_]{2,})", "Item"),
    (r"(Internet\s*#\s*[:\-]?\s*)([A-Z0-9][A-Z0-9\-/_]{2,})", "Internet"),
    (r"(\bSKU\b(?:\s*#)?\s*[:\-]?\s*)([A-Z0-9][A-Z0-9\-/_]{2,})", "SKU"),
]

def normalize_key(s: str) -> str:
    s = str(s).strip()
    if not ZERO_SIGNIFICANT:
        s = re.sub(r"^0+(?!$)", "", s)
    s = s.upper().replace(" ", "").replace("-", "").replace("_", "").replace("/", "")
    return s

def get_pdf_backend():
    """Return (name, PdfReader, PdfWriter) from PyPDF2 or pypdf."""
    try:
        from PyPDF2 import PdfReader, PdfWriter
        return "PyPDF2", PdfReader, PdfWriter
    except Exception:
        try:
            from pypdf import PdfReader, PdfWriter
            return "pypdf", PdfReader, PdfWriter
        except Exception as e:
            raise ImportError("No PDF backend installed. Need PyPDF2 or pypdf.") from e

def extract_candidates(text: str):
    cands = []
    if not text: return cands
    joined = text.replace("\r", " ").replace("\n", " ")
    for pat, label in ANCHOR_PATTERNS:
        for m in re.finditer(pat, joined, flags=re.IGNORECASE):
            token = m.group(2).strip().strip(":#")
            cands.append((label, token))
    return cands

def safe_name(s: str) -> str:
    return re.sub(r"[^\w\-]+", "_", s).strip("_")

def process_store_ui(store_label: str, store_key: str):
    st.header(store_label)
    pdfs = st.file_uploader(f"{store_label} PDFs", type=["pdf"], accept_multiple_files=True, key=f"pdfs_{store_key}")
    xls = st.file_uploader(f"{store_label} SKU→Vendor Excel", type=["xlsx","xls"], key=f"xls_{store_key}")
    run = st.button(f"Process {store_label}", key=f"run_{store_key}")

    if not run:
        return

    try:
        import pandas as pd
    except Exception as e:
        st.error(f"pandas import failed: {e}")
        return

    try:
        backend_name, PdfReader, PdfWriter = get_pdf_backend()
        st.info(f"Using PDF backend: **{backend_name}**")
    except Exception as e:
        st.error(f"PDF backend import failed: {e}")
        st.stop()

    if not pdfs or not xls:
        st.error("Please upload at least one PDF and a SKU→Vendor Excel.")
        return

    try:
        df = pd.read_excel(io.BytesIO(xls.read()), engine="openpyxl")
    except Exception as e:
        st.exception(e); return

    vendor_col = None
    for c in df.columns:
        if "vendor" in str(c).lower():
            vendor_col = c; break
    if vendor_col is None:
        st.error("Excel needs a 'Vendor' column (any case)."); return

    key_cols = [c for c in df.columns if any(k in str(c).lower() for k in ["sku","item","model","internet"])]
    if not key_cols:
        st.error("No key columns found in Excel (need SKU/Item/Model/Internet #)."); return

    mapping = {}
    for _, row in df.iterrows():
        vendor = str(row[vendor_col]).strip()
        if vendor in ("", "nan", "None"):
            continue
        for col in key_cols:
            v = str(row.get(col, "")).strip()
            if v and v not in ("nan", "None"):
                mapping[normalize_key(v)] = vendor
    keyset = set(mapping.keys())

    base = f"outputs/{store_key}"
    vendor_dir = os.path.join(base, "vendors"); os.makedirs(vendor_dir, exist_ok=True)
    unmatched_dir = os.path.join(base, "errors/unmatched"); os.makedirs(unmatched_dir, exist_ok=True)
    mixed_dir = os.path.join(base, "errors/mixed"); os.makedirs(mixed_dir, exist_ok=True)
    logs_dir = os.path.join(base, "logs"); os.makedirs(logs_dir, exist_ok=True)

    status = st.empty()
    progress = st.progress(0)
    total_pages = 0
    for pdf in pdfs:
        try:
            r = PdfReader(io.BytesIO(pdf.getvalue()))
            total_pages += len(r.pages)
        except Exception:
            pass
    done = 0

    vendor_writers = {}
    def ensure_writer(v):
        if v not in vendor_writers:
            vendor_writers[v] = PdfWriter()
        return vendor_writers[v]

    logs = []
    for pdf in pdfs:
        pb = pdf.getvalue()
        try:
            reader = PdfReader(io.BytesIO(pb))
        except Exception as e:
            st.error(f"Failed to open PDF {pdf.name}: {e}")
            continue

        for i, page in enumerate(reader.pages):
            status.text(f"Processing {pdf.name} page {i+1}/{len(reader.pages)} …")
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            cands = extract_candidates(text)
            matches = []
            for label, raw in cands:
                nz = normalize_key(raw)
                if nz in keyset:
                    order = [p[1] for p in ANCHOR_PATTERNS]
                    score = 1.0 - 0.1 * order.index(label) if label in order else 0.5
                    matches.append((raw, nz, mapping[nz], label, "native", score))

            vendors = list({m[2] for m in matches})
            if len(vendors) == 1:
                v = vendors[0]
                w = ensure_writer(v)
                w.add_page(reader.pages[i])
                best = sorted(matches, key=lambda x: -x[5])[0] if matches else None
                logs.append({"store": store_key, "source_pdf": pdf.name, "page_index": str(i),
                             "vendor": v, "raw_token": best[0] if best else "", "normalized": best[1] if best else "",
                             "anchor": best[3] if best else "", "method": best[4] if best else "", "confidence": f"{best[5]:.2f}" if best else "0.00"})
            elif len(vendors) > 1:
                single = PdfWriter(); single.add_page(reader.pages[i])
                with open(os.path.join(mixed_dir, f"{safe_name(pdf.name)}_p{i+1}.pdf"), "wb") as f:
                    single.write(f)
                logs.append({"store": store_key,"source_pdf": pdf.name,"page_index": str(i),"vendor":"MIXED",
                             "raw_token":"", "normalized":"", "anchor":"", "method":"", "confidence":"0.00"})
            else:
                single = PdfWriter(); single.add_page(reader.pages[i])
                with open(os.path.join(unmatched_dir, f"{safe_name(pdf.name)}_p{i+1}.pdf"), "wb") as f:
                    single.write(f)
                logs.append({"store": store_key,"source_pdf": pdf.name,"page_index": str(i),"vendor":"UNMATCHED",
                             "raw_token":"", "normalized":"", "anchor":"", "method":"", "confidence":"0.00"})

            done += 1
            if total_pages:
                progress.progress(min(done/total_pages, 1.0))

    # Write vendor PDFs
    for vendor, writer in vendor_writers.items():
        if len(writer.pages) == 0: continue
        with open(os.path.join(vendor_dir, f"{safe_name(vendor)}.pdf"), "wb") as f:
            writer.write(f)

    # Write logs CSV
    logs_path = os.path.join(logs_dir, "summary.csv")
    with open(logs_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["store","source_pdf","page_index","vendor","raw_token","normalized","anchor","method","confidence"])
        w.writeheader()
        for r in logs:
            w.writerow(r)

    # Zip per store
    zip_path = os.path.join(base, f"{store_key}_{__import__('datetime').datetime.now().strftime('%Y-%m-%d')}.zip")
    import zipfile
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(base):
            for file in files:
                full = os.path.join(root, file)
                zf.write(full, arcname=os.path.relpath(full, base))

    st.success(f"Done! Download ZIP for {store_label}:")
    with open(zip_path, "rb") as f:
        st.download_button(label=f"Download {os.path.basename(zip_path)}", data=f.read(), file_name=os.path.basename(zip_path), mime="application/zip")

# ---- UI tabs ----
tab1, tab2, tab3 = st.tabs(["Home Depot", "Lowe's", "Tractor Supply"])
with tab1: process_store_ui("Home Depot", "Depot")
with tab2: process_store_ui("Lowe's", "Lowes")
with tab3: process_store_ui("Tractor Supply", "TSC")
