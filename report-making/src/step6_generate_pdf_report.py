from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate,
    Paragraph, Spacer, PageBreak, Image,
    Table, TableStyle, ListFlowable, ListItem
)
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.lib.enums import TA_CENTER


STEP6_BLUEPRINT_JSON = os.getenv("STEP6_BLUEPRINT_JSON", "outputs/step5_report_blueprint.json")
STEP6_OUTPUT_PDF = os.getenv("STEP6_OUTPUT_PDF", "outputs/final_report.pdf")


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def make_styles():
    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(name="CoverTitle", parent=styles["Title"], alignment=TA_CENTER, fontSize=26, leading=30, spaceAfter=18))
    styles.add(ParagraphStyle(name="CoverSub", parent=styles["Normal"], alignment=TA_CENTER, fontSize=12, leading=16, spaceAfter=8))

    styles.add(ParagraphStyle(name="H1", parent=styles["Heading1"], fontSize=16, leading=20, spaceBefore=10, spaceAfter=8))
    styles.add(ParagraphStyle(name="H2", parent=styles["Heading2"], fontSize=13, leading=17, spaceBefore=8, spaceAfter=6))
    styles.add(ParagraphStyle(name="H3", parent=styles["Heading3"], fontSize=11.5, leading=15, spaceBefore=6, spaceAfter=4))

    styles.add(ParagraphStyle(name="TOCTitle", parent=styles["Heading1"], fontSize=16, leading=20, spaceBefore=0, spaceAfter=10))

    styles.add(ParagraphStyle(name="Body", parent=styles["Normal"], fontSize=10.5, leading=14))
    styles.add(ParagraphStyle(name="Caption", parent=styles["Normal"], fontSize=9, leading=12, textColor=colors.grey))
    return styles


class ReportDoc(BaseDocTemplate):
    def __init__(self, filename, **kwargs):
        super().__init__(filename, **kwargs)
        self._bookmark_id = 0

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph):
            style = flowable.style.name
            text = flowable.getPlainText()

            level = None
            if style == "H1":
                level = 0
            elif style == "H2":
                level = 1
            elif style == "H3":
                level = 2

            if level is not None:
                self._bookmark_id += 1
                key = f"bk_{self._bookmark_id}"
                self.canv.bookmarkPage(key)
                self.notify("TOCEntry", (level, text, self.page))


def _page_number(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(colors.grey)
    canvas.drawRightString(A4[0] - 2*cm, 1.2*cm, f"{doc.page}")
    canvas.restoreState()


def block_to_flowables(block: Dict[str, Any], styles, assets_root: Path) -> List[Any]:
    t = block.get("type")
    out: List[Any] = []

    if t == "heading":
        level = int(block.get("level", 2))
        text = str(block.get("text", "") or "")
        style = styles["H2"] if level == 2 else styles["H3"]
        out.append(Paragraph(text, style))
        return out

    if t == "paragraph":
        out.append(Paragraph(str(block.get("text", "") or ""), styles["Body"]))
        out.append(Spacer(1, 8))
        return out

    if t == "bullets":
        items = block.get("items", []) or []
        li = [ListItem(Paragraph(str(it), styles["Body"]), leftIndent=14) for it in items]
        out.append(ListFlowable(li, bulletType="bullet", leftIndent=18))
        out.append(Spacer(1, 8))
        return out

    if t == "metrics_strip":
        pairs = block.get("pairs", []) or []
        labels = [str(p[0]) for p in pairs]
        values = [str(p[1]) for p in pairs]
        data = [labels, values]
        tbl = Table(data, hAlign="LEFT")
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#efefef")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
        ]))
        out.append(tbl)
        out.append(Spacer(1, 12))
        return out

    if t == "table":
        from xml.sax.saxutils import escape

        headers = block.get("headers", []) or []
        rows = block.get("rows", []) or []

        avail_w = A4[0] - 4*cm

        # Column widths: special-case 4-column "Top speeches" style tables
        col_count = len(headers)
        if col_count == 4:
            col_widths = [1.2*cm, 2.6*cm, 2.2*cm, avail_w - (1.2*cm + 2.6*cm + 2.2*cm)]
        else:
            col_widths = [avail_w / max(1, col_count)] * max(1, col_count)

        cell_style = ParagraphStyle("Cell", parent=styles["Body"], fontSize=9, leading=11)
        head_style = ParagraphStyle("Head", parent=styles["Body"], fontSize=9, leading=11)
        head_style.fontName = "Helvetica-Bold"

        data = []
        data.append([Paragraph(escape(str(h)), head_style) for h in headers])

        for r in rows:
            data.append([Paragraph(escape(str(c)), cell_style) for c in r])

        tbl = Table(data, colWidths=col_widths, hAlign="CENTER")
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#efefef")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        out.append(tbl)
        out.append(Spacer(1, 10))
        return out

    if t == "image":
        img_path = str(block.get("path", "") or "")
        if not img_path:
            return out

        p = Path(img_path)
        if not p.is_absolute():
            p = assets_root / p

        if p.exists():
            max_w = A4[0] - 4*cm
            max_h = A4[1] - 9*cm
            img = Image(str(p))
            iw, ih = img.imageWidth, img.imageHeight
            if iw > 0 and ih > 0:
                scale = min(max_w/iw, max_h/ih)
                img.drawWidth = iw * scale
                img.drawHeight = ih * scale

            out.append(img)
            cap = str(block.get("caption", "") or "")
            if cap:
                out.append(Spacer(1, 4))
                out.append(Paragraph(cap, styles["Caption"]))
            out.append(Spacer(1, 12))
        else:
            out.append(Paragraph(f"[Missing image: {img_path}]", styles["Caption"]))
            out.append(Spacer(1, 8))
        return out

    return out


def main() -> None:
    blueprint_path = Path(STEP6_BLUEPRINT_JSON)
    if not blueprint_path.exists():
        raise FileNotFoundError(f"Blueprint JSON not found: {blueprint_path.resolve()}")

    blueprint = load_json(blueprint_path)
    styles = make_styles()

    out_pdf = Path(STEP6_OUTPUT_PDF)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    frame = Frame(2*cm, 2*cm, A4[0] - 4*cm, A4[1] - 4*cm, id="normal")
    template = PageTemplate(id="main", frames=[frame], onPage=_page_number)

    doc = ReportDoc(str(out_pdf), pagesize=A4)
    doc.addPageTemplates([template])

    story: List[Any] = []

    # Cover
    cover = blueprint.get("cover", {}) or {}
    story.append(Spacer(1, 3*cm))
    story.append(Paragraph(str(cover.get("title", "")), styles["CoverTitle"]))
    story.append(Paragraph(str(cover.get("subtitle", "")), styles["CoverSub"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(f"Date: {cover.get('date','')}", styles["CoverSub"]))

    kw = cover.get("keyword", "")
    window = cover.get("window", {}) or {}
    if kw:
        story.append(Paragraph(f"Keyword: “{kw}”", styles["CoverSub"]))
    if window:
        story.append(Paragraph(f"Window: {window.get('start_date','')} to {window.get('end_date','')}", styles["CoverSub"]))

    totals = cover.get("totals", {}) or {}
    if totals:
        story.append(Spacer(1, 10))
        story.append(Paragraph(f"Speeches: {totals.get('speeches','')} • Mentions: {totals.get('mentions','')}", styles["CoverSub"]))

    story.append(PageBreak())

    # Contents (TOC)
    story.append(Paragraph("Contents", styles["TOCTitle"]))
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle(fontName="Helvetica", fontSize=10.5, name="TOCLevel0", leftIndent=12, firstLineIndent=-12, spaceBefore=6),
        ParagraphStyle(fontName="Helvetica", fontSize=9.5, name="TOCLevel1", leftIndent=24, firstLineIndent=-12, spaceBefore=2),
        ParagraphStyle(fontName="Helvetica", fontSize=9.0, name="TOCLevel2", leftIndent=36, firstLineIndent=-12, spaceBefore=1),
    ]
    story.append(toc)
    story.append(PageBreak())

    assets_root = blueprint_path.parent.parent  # project root if outputs/...
    sections = blueprint.get("sections", []) or []

    for sec in sections:
        sec_id = sec.get("id", "")
        sec_title = sec.get("title", "")
        story.append(Paragraph(f"{sec_id}. {sec_title}", styles["H1"]))

        blocks = sec.get("blocks", []) or []
        for b in blocks:
            story.extend(block_to_flowables(b, styles, assets_root))

        story.append(PageBreak())

    # ✅ multiBuild is REQUIRED to populate TableOfContents
    doc.multiBuild(story)

    print("[DONE] PDF generated:", out_pdf.resolve())


if __name__ == "__main__":
    main()
