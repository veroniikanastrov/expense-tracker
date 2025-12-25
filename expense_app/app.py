import streamlit as st
import sqlite3
import re
from datetime import datetime
from dateutil import parser as date_parser
import pandas as pd
import io
from PIL import Image
import pdfplumber

DB_PATH = "expenses.db"

CATEGORIES = [
    "לא משויך",
    "פרסום ושיווק",
    "ציוד משרדי",
    "תוכנות ומנויים",
    "נסיעות וחניה",
    "אירוח וקפה",
    "שירותים מקצועיים",
    "תקשורת ואינטרנט",
    "אחר",
]

DATE_PATTERNS = [
    r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b",
    r"\b(\d{4}[./-]\d{1,2}[./-]\d{1,2})\b",
]

AMOUNT_PATTERNS = [
    r"(?:סה\"?כ\s*לתשלום|סה\"?כ\s*תשלום|סכום\s*לתשלום|לתשלום)\s*[:\-]?\s*₪?\s*([0-9][0-9,]*\.?[0-9]{0,2})",
    r"(?:סה\"?כ\s*כולל\s*מע\"?מ|סה\"?כ\s*כולל)\s*[:\-]?\s*₪?\s*([0-9][0-9,]*\.?[0-9]{0,2})",
    r"₪\s*([0-9][0-9,]*\.?[0-9]{0,2})",
]

VENDOR_HINTS = [
    r"שם\s*ספק\s*[:\-]?\s*(.+)",
    r"ספק\s*[:\-]?\s*(.+)",
    r"לכבוד\s*(.+)",
]

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT,
                doc_date TEXT,
                amount_ils REAL,
                vendor TEXT,
                category TEXT,
                notes TEXT,
                created_at TEXT
            )
        """)
        conn.commit()

def insert_expense(filename: str, doc_date: str, amount_ils: float, vendor: str, category: str, notes: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO expenses (filename, doc_date, amount_ils, vendor, category, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (filename, doc_date, amount_ils, vendor, category, notes, datetime.now().isoformat(timespec="seconds"))
        )
        conn.commit()

def update_expense(expense_id: int, doc_date: str, amount_ils: float, vendor: str, category: str, notes: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE expenses SET doc_date=?, amount_ils=?, vendor=?, category=?, notes=? WHERE id=?",
            (doc_date, amount_ils, vendor, category, notes, expense_id)
        )
        conn.commit()

def delete_expense(expense_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
        conn.commit()

def fetch_expenses() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query("SELECT * FROM expenses ORDER BY doc_date ASC, id ASC", conn)
    if not df.empty:
        df["doc_date"] = pd.to_datetime(df["doc_date"], errors="coerce")
    return df

def require_login():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return True

    st.title("התחברות")
    st.write("האפליקציה פרטית. כדי להיכנס צריך סיסמה.")

    app_pwd = st.secrets.get("APP_PASSWORD", "")
    if not app_pwd:
        st.error("לא הוגדרה סיסמה. ב-Streamlit Cloud: Settings -> Secrets והוסיפי APP_PASSWORD.")
        st.stop()

    pwd = st.text_input("סיסמה", type="password")
    if st.button("כניסה"):
        if pwd == app_pwd:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("סיסמה שגויה")

    st.stop()

def normalize_amount(s: str):
    try:
        s = s.replace(",", "").strip()
        return float(s)
    except Exception:
        return None

def try_parse_date(text: str):
    if not text:
        return None
    candidates = []
    for pat in DATE_PATTERNS:
        for m in re.finditer(pat, text):
            candidates.append(m.group(1))

    parsed = []
    for c in candidates:
        try:
            dt = date_parser.parse(c, dayfirst=True, fuzzy=True)
            if 2000 <= dt.year <= 2100:
                parsed.append(dt)
        except Exception:
            pass

    if not parsed:
        return None

    parsed.sort()
    return parsed[0].date().isoformat()

def try_parse_amount(text: str):
    if not text:
        return None
    for pat in AMOUNT_PATTERNS:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            amt = normalize_amount(m.group(1))
            if amt is not None and amt >= 0:
                return amt
    return None

def try_parse_vendor(text: str):
    if not text:
        return ""
    for pat in VENDOR_HINTS:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            v = m.group(1).strip()
            v = re.sub(r"\s{2,}", " ", v)
            return v[:80]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return (lines[0][:80] if lines else "")

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[:5]:
            t = page.extract_text() or ""
            if t.strip():
                text_parts.append(t)
    return "\n".join(text_parts).strip()

def ils(n):
    try:
        return f"₪{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return ""

def main():
    st.set_page_config(page_title="מעקב הוצאות", layout="wide")
    init_db()
    require_login()

    st.title("מעקב הוצאות לפי חודש")

    tab1, tab2 = st.tabs(["העלאה והוספה", "דשבורד וסיכומים"])

    with tab1:
        st.subheader("העלאת חשבונית (PDF או תמונה)")
        st.caption("ב-PDF רגיל ננסה לזהות תאריך וסכום אוטומטית. בתמונה, לרוב ממלאים ידנית.")
        uploaded = st.file_uploader("בחרי קובץ", type=["pdf", "png", "jpg", "jpeg"], accept_multiple_files=False)

        extracted_text = ""
        guess_date = None
        guess_amount = None
        guess_vendor = ""

        if uploaded is not None:
            file_bytes = uploaded.read()
            filename = uploaded.name
            is_pdf = filename.lower().endswith(".pdf")

            if is_pdf:
                extracted_text = extract_text_from_pdf(file_bytes)
                if extracted_text:
                    guess_date = try_parse_date(extracted_text)
                    guess_amount = try_parse_amount(extracted_text)
                    guess_vendor = try_parse_vendor(extracted_text)
            else:
                extracted_text = ""

            st.markdown("**מילוי פרטים לפני שמירה**")
            col1, col2, col3 = st.columns([1, 1, 1])
            with col1:
                doc_date = st.text_input("תאריך חשבונית (YYYY-MM-DD)", value=guess_date or "")
            with col2:
                amount = st.number_input("סכום בש״ח", min_value=0.0, value=float(guess_amount) if guess_amount else 0.0, step=1.0)
            with col3:
                vendor = st.text_input("ספק (רשות)", value=guess_vendor or "")

            col4, col5 = st.columns([1, 2])
            with col4:
                category = st.selectbox("קטגוריה", options=CATEGORIES, index=0)
            with col5:
                notes = st.text_input("הערות (רשות)", value="")

            save = st.button("שמירה לרשימת ההוצאות", type="primary")
            if save:
                if not doc_date:
                    st.error("חסר תאריך. הזיני תאריך בפורמט YYYY-MM-DD.")
                else:
                    try:
                        datetime.fromisoformat(doc_date)
                        insert_expense(filename, doc_date, float(amount), vendor, category, notes)
                        st.success("נשמר! אפשר לעבור לדשבורד.")
                    except Exception:
                        st.error("תאריך לא תקין. פורמט נכון: YYYY-MM-DD")

        st.divider()
        st.subheader("הוצאות קיימות (עריכה ומחיקה)")
        df = fetch_expenses()
        if df.empty:
            st.info("עדיין אין הוצאות שמורות.")
        else:
            df_show = df.copy()
            df_show["חודש"] = df_show["doc_date"].dt.to_period("M").astype(str)
            df_show["סכום"] = df_show["amount_ils"].apply(ils)
            df_show = df_show.rename(columns={
                "doc_date": "תאריך",
                "vendor": "ספק",
                "category": "קטגוריה",
                "notes": "הערות",
                "filename": "קובץ",
                "created_at": "נוצר"
            })
            st.dataframe(
                df_show[["id", "חודש", "תאריך", "סכום", "ספק", "קטגוריה", "הערות", "קובץ", "נוצר"]],
                use_container_width=True,
                hide_index=True
            )

            st.caption("כדי לערוך או למחוק, בחרי מזהה (id).")
            exp_id = st.number_input("id לעריכה/מחיקה", min_value=1, value=int(df["id"].iloc[0]))
            row = df[df["id"] == exp_id]
            if row.empty:
                st.warning("לא נמצא id כזה.")
            else:
                r = row.iloc[0]
                with st.form("edit_form"):
                    c1, c2, c3 = st.columns([1, 1, 1])
                    with c1:
                        new_date = st.text_input(
                            "תאריך (YYYY-MM-DD)",
                            value=r["doc_date"].date().isoformat() if pd.notna(r["doc_date"]) else ""
                        )
                    with c2:
                        new_amount = st.number_input("סכום בש״ח", min_value=0.0, value=float(r["amount_ils"] or 0.0), step=1.0)
                    with c3:
                        new_vendor = st.text_input("ספק", value=r["vendor"] or "")

                    c4, c5 = st.columns([1, 2])
                    with c4:
                        idx = CATEGORIES.index(r["category"]) if r["category"] in CATEGORIES else 0
                        new_category = st.selectbox("קטגוריה", options=CATEGORIES, index=idx)
                    with c5:
                        new_notes = st.text_input("הערות", value=r["notes"] or "")

                    colA, colB = st.columns([1, 1])
                    with colA:
                        submitted = st.form_submit_button("שמירת שינויים")
                    with colB:
                        delete = st.form_submit_button("מחיקה")

                if submitted:
                    try:
                        datetime.fromisoformat(new_date)
                        update_expense(int(exp_id), new_date, float(new_amount), new_vendor, new_category, new_notes)
                        st.success("עודכן.")
                        st.rerun()
                    except Exception:
                        st.error("תאריך לא תקין. פורמט: YYYY-MM-DD")

                if delete:
                    delete_expense(int(exp_id))
                    st.success("נמחק.")
                    st.rerun()

    with tab2:
        st.subheader("סיכום הוצאות לפי חודש")
        df = fetch_expenses()
        if df.empty:
            st.info("אין נתונים להצגה עדיין.")
            return

        df2 = df.copy()
        df2 = df2[pd.notna(df2["doc_date"])]
        df2["month"] = df2["doc_date"].dt.to_period("M").astype(str)

        monthly = df2.groupby("month", as_index=False)["amount_ils"].sum().sort_values("month")
        monthly["סהכ הוצאות"] = monthly["amount_ils"].apply(ils)

        left, right = st.columns([1, 2])
        with left:
            st.markdown("**סהכ לפי חודש**")
            st.dataframe(
                monthly[["month", "סהכ הוצאות"]].rename(columns={"month": "חודש"}),
                use_container_width=True,
                hide_index=True
            )

        with right:
            st.markdown("**פירוט לפי חודש**")
            months = monthly["month"].tolist()
            selected = st.selectbox("בחרי חודש", options=months, index=len(months) - 1)
            sub = df2[df2["month"] == selected].copy()
            sub["סכום"] = sub["amount_ils"].apply(ils)
            sub = sub.rename(columns={"doc_date": "תאריך", "vendor": "ספק", "category": "קטגוריה", "notes": "הערות", "filename": "קובץ"})
            st.dataframe(sub[["תאריך", "סכום", "ספק", "קטגוריה", "הערות", "קובץ"]], use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("ייצוא CSV")
        export = df2.copy()
        export["doc_date"] = export["doc_date"].dt.date.astype(str)
        csv = export.to_csv(index=False).encode("utf-8-sig")
        st.download_button("הורדת CSV", data=csv, file_name="expenses_export.csv", mime="text/csv")

if __name__ == "__main__":
    main()
