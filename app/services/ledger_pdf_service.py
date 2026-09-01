"""Client-ledger PDF statements.

Renders the exact data the ledger modal shows (same entries, same running
balance, same B/F semantics for windowed views) as an A4 statement.

Fonts are resolved from the machine at runtime — DejaVu on the Linux
droplet, Segoe UI / Arial on a Windows dev box. When no Unicode font is
found the statement falls back to the core Helvetica font and degrades
"₹" to "Rs" so latin-1 encoding never crashes it.
"""

import os
import re
from datetime import datetime

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from .. import _format_inr

# (regular, bold) candidate pairs; the first pair fully present on disk wins.
_STATIC_FONTS = os.path.join(os.path.dirname(__file__), "..", "static", "fonts")
_FONT_CANDIDATES = [
    # Repo-bundled fonts take priority (drop DejaVuSans[-Bold].ttf there to override).
    (os.path.join(_STATIC_FONTS, "DejaVuSans.ttf"),
     os.path.join(_STATIC_FONTS, "DejaVuSans-Bold.ttf")),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("C:/Windows/Fonts/arial.ttf",   "C:/Windows/Fonts/arialbd.ttf"),
]

# Column widths in mm (A4 portrait, 12 mm margins → 186 mm usable).
_W_DATE, _W_DEBIT, _W_CREDIT, _W_BAL = 22, 26, 26, 32
_W_DESC = 186 - _W_DATE - _W_DEBIT - _W_CREDIT - _W_BAL

_LINE_H = 4.6   # one text line inside a row
_PAD_Y  = 1.7   # vertical cell padding

_COL_DEBIT   = (185, 40, 40)
_COL_CREDIT  = (13, 130, 90)
_COL_MUTED   = (120, 126, 138)
_COL_TEXT    = (24, 26, 32)
_COL_ZEBRA   = (247, 248, 250)
_COL_BF_FILL = (236, 236, 248)
_COL_RULE    = (222, 225, 230)


def _find_fonts():
    for regular, bold in _FONT_CANDIDATES:
        if os.path.isfile(regular) and os.path.isfile(bold):
            return regular, bold
    return None


def _dmy(iso):
    """'2026-07-27' → '27-07-2026' (leave anything unparseable untouched)."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d-%m-%Y")
    except (TypeError, ValueError):
        return iso or ""


class _LedgerPDF(FPDF):
    def __init__(self, title, subtitle, period_label, final_balance):
        super().__init__("P", "mm", "A4")
        self.doc_title     = title
        self.doc_subtitle  = subtitle
        self.period_label  = period_label
        self.final_balance = final_balance

        fonts = _find_fonts()
        self.unicode = fonts is not None
        if self.unicode:
            self.add_font("Ledger", "", fonts[0])
            self.add_font("Ledger", "B", fonts[1])
            self.font_family_name = "Ledger"
        else:
            self.font_family_name = "Helvetica"

        self.set_margins(12, 12, 12)
        self.set_auto_page_break(True, margin=18)
        self.alias_nb_pages()

    # ── text helpers ─────────────────────────────────────────────
    def _txt(self, s):
        """Make a string safe for the active font."""
        s = str(s or "")
        if not self.unicode:
            s = s.replace("₹", "Rs ").replace("—", "-")
            s = s.encode("latin-1", "replace").decode("latin-1")
        return s

    def money(self, value, dr_cr=False):
        s = _format_inr(abs(value))
        if not self.unicode:
            s = s.replace("₹", "Rs ")
        if dr_cr and round(value, 2) != 0:
            s += " Dr" if value < 0 else " Cr"
        return s

    def _fit(self, text, width, size, min_size=6.5):
        """Shrink font size until text fits in width; returns the size to use."""
        while size > min_size and self.get_string_width(text) > width - 2:
            size -= 0.5
            self.set_font_size(size)
        return size

    def _amount_cell(self, w, text, color, bold=False, fill=False):
        self.set_font(self.font_family_name, "B" if bold else "", 9)
        self.set_text_color(*color)
        self._fit(text, w, 9)
        self.cell(w, _LINE_H, text, align="R", fill=fill,
                  new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.set_font(self.font_family_name, "", 9)

    # ── page furniture ───────────────────────────────────────────
    def header(self):
        if self.page_no() == 1:
            bal = self.final_balance
            bal_color = _COL_DEBIT if bal < 0 else _COL_CREDIT if bal > 0 else _COL_MUTED

            self.set_font(self.font_family_name, "", 9)
            self.set_text_color(*_COL_MUTED)
            self.cell(120, 4.5, self._txt(self.doc_subtitle), new_x=XPos.RIGHT, new_y=YPos.TOP)
            self.cell(66, 4.5, "CLOSING BALANCE", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

            y = self.get_y()
            self.set_font(self.font_family_name, "B", 15)
            self.set_text_color(*_COL_TEXT)
            title = self._txt(self.doc_title)
            self._fit(title, 120, 15, min_size=10)
            self.cell(120, 7.5, title, new_x=XPos.RIGHT, new_y=YPos.TOP)

            self.set_font(self.font_family_name, "B", 13)
            self.set_text_color(*bal_color)
            self.cell(66, 7.5, self.money(bal, dr_cr=True), align="R",
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_y(y + 8.5)

            self.set_font(self.font_family_name, "", 9)
            self.set_text_color(*_COL_MUTED)
            self.cell(0, 5, self._txt(f"Period: {self.period_label}"),
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(2.5)

        # Column header row (every page)
        self.set_font(self.font_family_name, "B", 7.6)
        self.set_text_color(*_COL_MUTED)
        self.set_fill_color(240, 242, 245)
        self.set_draw_color(*_COL_RULE)
        h = 6.4
        self.cell(_W_DATE,   h, "DATE",          border="B", fill=True, new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.cell(_W_DESC,   h, "DESCRIPTION",   border="B", fill=True, new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.cell(_W_DEBIT,  h, "DEBIT (OWED)",  border="B", fill=True, align="R", new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.cell(_W_CREDIT, h, "CREDIT (PAID)", border="B", fill=True, align="R", new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.cell(_W_BAL,    h, "BALANCE",       border="B", fill=True, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def footer(self):
        self.set_y(-13)
        self.set_draw_color(*_COL_RULE)
        self.line(12, self.get_y(), 198, self.get_y())
        self.set_y(-11)
        self.set_font(self.font_family_name, "", 7.5)
        self.set_text_color(*_COL_MUTED)
        stamp = datetime.now().strftime("%d-%m-%Y %H:%M")
        self.cell(120, 5, self._txt(f"Generated {stamp} - Ledger"), new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.cell(66, 5, f"Page {self.page_no()}/{{nb}}", align="R")

    # ── table body ───────────────────────────────────────────────
    def render_rows(self, entries):
        self.set_font(self.font_family_name, "", 9)

        if not entries:
            self.ln(10)
            self.set_text_color(*_COL_MUTED)
            self.cell(0, 8, "No transactions in this period.", align="C",
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            return

        total_debit = total_credit = 0.0

        for idx, e in enumerate(entries):
            label = self._txt(e.get("label") or "")
            lines = self.multi_cell(_W_DESC - 2, _LINE_H, label,
                                    dry_run=True, output="LINES")
            if len(lines) > 4:
                lines = lines[:4]
                lines[-1] = lines[-1][:max(len(lines[-1]) - 1, 0)] + "…" if self.unicode \
                    else lines[-1][:max(len(lines[-1]) - 3, 0)] + "..."
            row_h = len(lines) * _LINE_H + 2 * _PAD_Y

            if self.will_page_break(row_h):
                self.add_page()

            is_carry = e.get("type") in ("bbf", "opening")
            if is_carry:
                self.set_fill_color(*_COL_BF_FILL)
            else:
                self.set_fill_color(*_COL_ZEBRA)
            fill = is_carry or idx % 2 == 1

            x0, y0 = self.l_margin, self.get_y()
            if fill:
                self.rect(x0, y0, 186, row_h, style="F")

            # Date
            self.set_xy(x0, y0 + _PAD_Y)
            self.set_text_color(*_COL_MUTED)
            self.cell(_W_DATE, _LINE_H, _dmy(e.get("date")) or "-",
                      new_x=XPos.RIGHT, new_y=YPos.TOP)

            # Description (wrapped)
            self.set_xy(x0 + _W_DATE, y0 + _PAD_Y)
            self.set_text_color(*_COL_TEXT)
            if is_carry:
                self.set_font(self.font_family_name, "B", 9)
            self.multi_cell(_W_DESC - 2, _LINE_H, "\n".join(lines))
            self.set_font(self.font_family_name, "", 9)

            # Amounts
            debit   = float(e.get("debit")  or 0)
            credit  = float(e.get("credit") or 0)
            running = float(e.get("running") or 0)
            total_debit  += debit
            total_credit += credit

            self.set_xy(x0 + _W_DATE + _W_DESC, y0 + _PAD_Y)
            self._amount_cell(_W_DEBIT, self.money(debit) if debit else "-",
                              _COL_DEBIT if debit else _COL_MUTED)
            self._amount_cell(_W_CREDIT, self.money(credit) if credit else "-",
                              _COL_CREDIT if credit else _COL_MUTED)
            bal_color = _COL_DEBIT if running < 0 else _COL_CREDIT if running > 0 else _COL_MUTED
            self._amount_cell(_W_BAL, self.money(running, dr_cr=True), bal_color, bold=True)

            self.set_y(y0 + row_h)
            self.set_draw_color(*_COL_RULE)
            self.line(x0, self.get_y(), x0 + 186, self.get_y())

        # Totals + closing balance band
        band_h = 2 * (_LINE_H + 2 * _PAD_Y)
        if self.will_page_break(band_h):
            self.add_page()

        y0 = self.get_y()
        self.set_xy(self.l_margin, y0 + _PAD_Y)
        self.set_font(self.font_family_name, "B", 9)
        self.set_text_color(*_COL_TEXT)
        self.cell(_W_DATE + _W_DESC, _LINE_H, "Totals", new_x=XPos.RIGHT, new_y=YPos.TOP)
        self._amount_cell(_W_DEBIT,  self.money(total_debit),  _COL_DEBIT,  bold=True)
        self._amount_cell(_W_CREDIT, self.money(total_credit), _COL_CREDIT, bold=True)
        self.set_y(y0 + _LINE_H + 2 * _PAD_Y)

        bal = self.final_balance
        bal_color = _COL_DEBIT if bal < 0 else _COL_CREDIT if bal > 0 else _COL_MUTED
        y0 = self.get_y()
        self.set_fill_color(*_COL_BF_FILL)
        self.rect(self.l_margin, y0, 186, _LINE_H + 2 * _PAD_Y, style="F")
        self.set_xy(self.l_margin, y0 + _PAD_Y)
        self.set_font(self.font_family_name, "B", 9.5)
        self.set_text_color(*_COL_TEXT)
        label = "Closing Balance"
        if bal < 0:
            label += " (client owes)"
        elif bal > 0:
            label += " (credit)"
        self.cell(_W_DATE + _W_DESC + _W_DEBIT, _LINE_H, label,
                  new_x=XPos.RIGHT, new_y=YPos.TOP)
        text = self.money(bal, dr_cr=True)
        self.set_text_color(*bal_color)
        self._fit(text, _W_CREDIT + _W_BAL, 9.5)
        self.cell(_W_CREDIT + _W_BAL, _LINE_H, text, align="R")
        self.set_y(y0 + _LINE_H + 2 * _PAD_Y)


def build_ledger_pdf(data):
    """data is the dict from clients._compute_ledger.

    Returns (pdf_bytes, filename).
    """
    client_name  = data.get("client_name") or "Client"
    company_name = data.get("company_name")
    date_from    = data.get("date_from")
    date_to      = data.get("date_to")

    title    = company_name or client_name
    subtitle = f"Company Ledger - {client_name}" if company_name else "Client Ledger"
    period   = f"{_dmy(date_from)} to {_dmy(date_to)}" if date_from and date_to else "All Time"

    pdf = _LedgerPDF(title, subtitle, period, float(data.get("final_balance") or 0))
    pdf.add_page()
    pdf.render_rows(data.get("entries") or [])

    safe = re.sub(r'[\\/:*?"<>|]+', "", title).strip() or "Client"
    filename = f"{safe} Ledger {period}.pdf"
    return bytes(pdf.output()), filename
