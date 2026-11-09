"""[Frontend&Reporting Engineer] Rapor çıktı biçimleri: HTML (Jinja2), PDF (reportlab), Excel (openpyxl), PNG."""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).parent / "templates"
FIG_SIZES = {"before_after": (1500, 650), "kpi_deltas": (1000, None)}
DEFAULT_FIG_SIZE = (1000, 650)


def fmt(v: Any) -> str:
    """Tablo hücresi biçimleyici (görüntü için; ham değerler ctx'te kalır)."""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "Evet" if v else "Hayır"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def figure_png(ctx: dict, key: str) -> bytes:
    """Plotly figürünü PNG'ye çevirir (kaleido); sonuç ctx içinde önbelleğe alınır."""
    cache: dict[str, bytes] = ctx["_png"]
    if key not in cache:
        fig = ctx["_figures"][key]["fig"]
        w, h = FIG_SIZES.get(key, DEFAULT_FIG_SIZE)
        kw: dict[str, Any] = {"format": "png", "width": w, "scale": 1.5}
        if h is not None:
            kw["height"] = h
        elif fig.layout.height is None:
            kw["height"] = DEFAULT_FIG_SIZE[1]
        cache[key] = fig.to_image(**kw)
    return cache[key]


def _out_name(ctx: dict, ext: str) -> str:
    return f"report_{ctx['project_id']}.{ext}"


# --------------------------------------------------------------------------- HTML
def export_html(ctx: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html", "j2"]))
    env.filters["fmt"] = fmt
    used = {k for s in ctx["sections"] for k in s["figures"]}
    figs = {k: {"caption": ctx["_figures"][k]["caption"],
                "b64": base64.b64encode(figure_png(ctx, k)).decode("ascii")} for k in used}
    html = env.get_template("report.html.j2").render(ctx=ctx, figs=figs)
    path = out_dir / _out_name(ctx, "html")
    path.write_text(html, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- PDF
def _register_font() -> tuple[str, str]:
    """Türkçe karakterler için DejaVu Sans (matplotlib ile birlikte gelir)."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if "DejaVuSans" not in pdfmetrics.getRegisteredFontNames():
        import matplotlib

        base = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        pdfmetrics.registerFont(TTFont("DejaVuSans", str(base / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(base / "DejaVuSans-Bold.ttf")))
    return "DejaVuSans", "DejaVuSans-Bold"


def export_pdf(ctx: dict, out_dir: Path) -> Path:
    from xml.sax.saxutils import escape

    from PIL import Image as PILImage
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Image,
        KeepTogether,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    reg, bold = _register_font()
    body = ParagraphStyle("body", fontName=reg, fontSize=9, leading=12)
    cell = ParagraphStyle("cell", fontName=reg, fontSize=7, leading=8.5)
    cellh = ParagraphStyle("cellh", fontName=bold, fontSize=7, leading=8.5)
    h1 = ParagraphStyle("h1", fontName=bold, fontSize=16, leading=20, spaceAfter=6)
    h2 = ParagraphStyle("h2", fontName=bold, fontSize=12, leading=15, spaceBefore=10, spaceAfter=4,
                        textColor=colors.HexColor("#2b6cb0"))
    cap = ParagraphStyle("cap", fontName=bold, fontSize=8, leading=10, spaceBefore=4, spaceAfter=2)
    label = ParagraphStyle("label", parent=body, textColor=colors.HexColor("#7b341e"), fontName=bold)

    page = landscape(A4)
    avail_w = page[0] - 30 * mm
    path = out_dir / _out_name(ctx, "pdf")
    doc = SimpleDocTemplate(str(path), pagesize=page, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm,
                            bottomMargin=15 * mm, title=ctx["title"], author="FactoryFlow AI")
    story: list[Any] = [Paragraph(escape(ctx["title"]), h1),
                        Paragraph(escape(f"Oluşturulma (UTC): {ctx['generated_at']} · Proje: {ctx['project_id']}"), body)]
    if ctx["data_label"]:
        story += [Spacer(1, 4), Paragraph(escape(ctx["data_label"]), label)]
    for s in ctx["sections"]:
        story.append(Paragraph(escape(f"{s['no']}. {s['title']}"), h2))
        for p in s["paragraphs"]:
            story.append(Paragraph(escape(p), body))
        for b in s["bullets"]:
            story.append(Paragraph("• " + escape(b), body))
        for t in s["tables"]:
            story.append(Paragraph(escape(t["caption"]), cap))
            data = [[Paragraph(escape(str(c)), cellh) for c in t["columns"]]]
            data += [[Paragraph(escape(fmt(v)), cell) for v in row] for row in t["rows"]]
            tbl = Table(data, repeatRows=1, colWidths=[avail_w / len(t["columns"])] * len(t["columns"]))
            tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e0")),
                                     ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf2f7")),
                                     ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story.append(tbl)
        for key in s["figures"]:
            png = figure_png(ctx, key)
            w, h = PILImage.open(io.BytesIO(png)).size
            width = min(avail_w, 230 * mm)
            height = width * h / w
            max_h = page[1] - 55 * mm
            if height > max_h:
                width, height = width * max_h / height, max_h
            story.append(KeepTogether([Image(io.BytesIO(png), width=width, height=height),
                                       Paragraph(escape(ctx["_figures"][key]["caption"]), cap)]))
    doc.build(story)
    return path


# --------------------------------------------------------------------------- Excel
def _write_table(ws: Any, t: dict, row0: int) -> int:
    from openpyxl.styles import Font, PatternFill

    ws.cell(row=row0, column=1, value=t["caption"]).font = Font(bold=True)
    for j, c in enumerate(t["columns"], start=1):
        cell = ws.cell(row=row0 + 1, column=j, value=c)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="EDF2F7")
    for i, row in enumerate(t["rows"], start=row0 + 2):
        for j, v in enumerate(row, start=1):
            ws.cell(row=i, column=j, value=v if isinstance(v, (int, float, str, bool)) or v is None else str(v))
    return row0 + 2 + len(t["rows"]) + 1


def _autowidth(ws: Any) -> None:
    from openpyxl.utils import get_column_letter

    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(10, width + 2), 60)


def export_excel(ctx: dict, out_dir: Path) -> Path:
    from openpyxl import Workbook

    out_dir.mkdir(parents=True, exist_ok=True)
    tables = {t["key"]: t for s in ctx["sections"] for t in s["tables"]}
    notes = {s["id"]: s for s in ctx["sections"]}
    wb = Workbook()
    wb.remove(wb.active)

    def sheet(name: str, keys: list[str], note_from: str | None = None) -> None:
        ws = wb.create_sheet(name)
        r = 1
        if ctx["data_label"]:
            ws.cell(row=r, column=1, value=ctx["data_label"])
            r += 2
        present = [tables[k] for k in keys if k in tables]
        for t in present:
            r = _write_table(ws, t, r)
        if not present and note_from:
            for p in notes[note_from]["paragraphs"] or ["(veri yok)"]:
                ws.cell(row=r, column=1, value=p)
                r += 1
        _autowidth(ws)

    sheet("Proje", ["project_info", "sim_config", "resources", "products"])
    sheet("KPI", ["kpis", "improvement", "savings", "ci95"], note_from="improvement_kpis")
    sheet("İstasyonlar", ["stations", "station_stats", "resource_stats", "validation"])
    sheet("From-To", ["from_to", "vehicle_distance"])
    sheet("Darboğaz", ["bottleneck_ranking", "resource_constraints"])
    sheet("Olay Özeti", ["event_summary"])
    sheet("Alternatifler", ["alternatives", "moved"], note_from="proposed_layout")
    sheet("Karşılaştırma", ["comparison"], note_from="scenario_comparison")
    sheet("Metodoloji", ["methodology"])
    path = out_dir / _out_name(ctx, "xlsx")
    wb.save(path)
    return path


# --------------------------------------------------------------------------- PNG
def export_png(ctx: dict, out_dir: Path) -> list[Path]:
    """Layout, spaghetti (+ varsa önerilen layout ve önce/sonra) PNG dosyaları."""
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = {"layout_current": "layout_current", "spaghetti_current": "spaghetti_current",
              "layout_proposed": "layout_proposed", "before_after": "before_after"}
    paths: list[Path] = []
    for key, stem in wanted.items():
        if key in ctx["_figures"]:
            p = out_dir / f"{stem}_{ctx['project_id']}.png"
            p.write_bytes(figure_png(ctx, key))
            paths.append(p)
    return paths
